"""네 가지 검색기의 Hit@3·Hit@5 성능 평가 실행 스크립트."""

import argparse
import json
import sys
from pathlib import Path


# 1. 프로젝트 루트 및 모듈 경로
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evaluation.retrieval_metrics import (
    load_eval_dataset,
    is_hit,
    is_value_hit,
    is_page_value_hit,
    calculate_hit_rate,
    summarize_value_hits,
)

from fin_research.retrieval.bm25_retriever import (
    load_chunk_documents,
    build_bm25_retriever,
    search_bm25,
)
from fin_research.retrieval.chroma_retriever import (
    create_chroma,
    search_mmr,
)
from fin_research.retrieval.hybrid_retriever import rrf_merge
from fin_research.retrieval.search_target import SearchTarget


# 2. 기본 경로
# DEFAULT_DATASET_PATH = (
#     PROJECT_ROOT / "evaluation" / "dataset" / "eval_retrieval.json"
# )

# DEFAULT_CHUNKS_PATH = (
#     PROJECT_ROOT / "data" / "processed" / "dart"
#     / "00126380" / "2025" / "20260310002820_chunks.json"
# )

DEFAULT_CHROMA_PATH = PROJECT_ROOT / "data" / "chroma_multi"

# DEFAULT_OUTPUT_PATH = (
#     PROJECT_ROOT
#     / "evaluation"
#     / "results"
#     / "retrieval_13_4_samsung_2025.json"
# )


def evaluate_retrievers(
    dataset_path: Path,
    chunks_path: Path,
    chroma_path: Path,
    target: SearchTarget,
) -> dict:
    """동일 평가셋으로 네 검색 방식을 비교한다."""

    questions = load_eval_dataset(dataset_path)
    documents = load_chunk_documents(chunks_path)

    # 값 지표 판정에 쓸 청크 텍스트 (chunk_id -> 본문)
    chunk_texts = {
        str(document.metadata["chunk_id"]): document.page_content
        for document in documents
    }


    # 선택한 검색 대상과 입력 청크 메타데이터 비교
    expected_source = f"{target.rcept_no}.pdf"

    for document in documents:
        metadata = document.metadata

        if metadata["fiscal_year"] != target.fiscal_year:
            raise ValueError(
                f"사업연도 불일치: {metadata['chunk_id']}"
            )

        if metadata["statement_scope"] != target.statement_scope:
            raise ValueError(
                f"연결/별도 기준 불일치: {metadata['chunk_id']}"
            )

        if metadata["source_file"] != expected_source:
            raise ValueError(
                f"공시번호 불일치: {metadata['chunk_id']}"
            )


    # BM25는 평가 전체에서 한 번만 구성
    bm25 = build_bm25_retriever(documents, k=10)

    # Chroma와 임베딩 모델도 한 번만 로드
    vectorstore = create_chroma(
        persist_directory=chroma_path,
        collection_name=target.collection_name,
    )

    if vectorstore._collection.count() != len(documents):
        raise ValueError(
            "Chroma와 입력 청크 수가 다릅니다. "
            "동일한 데이터로 색인했는지 확인하세요."
        )

    stored_ids = set(vectorstore.get(include=[])["ids"])
    input_ids = {str(doc.metadata["chunk_id"]) for doc in documents}

    if stored_ids != input_ids:
        raise ValueError(
            "Chroma 청크 ID가 입력 JSON과 일치하지 않습니다."
        )

    methods = ["bm25", "chroma", "mmr", "hybrid"]
    evaluations = {method: [] for method in methods}

    print(f"평가 질문: {len(questions)}개")
    print(f"검색 청크: {len(documents)}개\n")

    for index, item in enumerate(questions, start=1):
        question = item["question"]
        gold_sources = item["gold_sources"]

        # 3. 키워드 검색: Hybrid 후보 10개까지 확보
        bm25_results = search_bm25(bm25, question)

        # 4. 벡터 검색: 질문 임베딩 후 상위 10개 확보
        chroma_hits = vectorstore.similarity_search_with_score(
            query=question,
            k=10,
        )

        chroma_results = []

        for rank, (doc, distance) in enumerate(chroma_hits, start=1):
            chroma_results.append({
                "rank": rank,
                "chunk_id": doc.metadata["chunk_id"],
                "text": doc.page_content,
                "source_file": doc.metadata["source_file"],
                "pdf_page": doc.metadata["pdf_page"],
                "distance": float(distance),
                "metadata": doc.metadata,
            })

        # 5. MMR 검색: 기존 구현 재사용
        # search_mmr()는 자체적으로 Chroma를 다시 여므로
        # 여기서는 같은 vectorstore를 사용해 모델 로딩을 줄임
        mmr_docs = vectorstore.max_marginal_relevance_search(
            query=question,
            k=5,
            fetch_k=20,
            lambda_mult=0.5,
        )

        mmr_results = []

        for rank, doc in enumerate(mmr_docs, start=1):
            mmr_results.append({
                "rank": rank,
                "chunk_id": doc.metadata["chunk_id"],
                "text": doc.page_content,
                "source_file": doc.metadata["source_file"],
                "pdf_page": doc.metadata["pdf_page"],
                "metadata": doc.metadata,
            })

        # 6. Hybrid-RRF: BM25 + Chroma Top-10 결합
        hybrid_results = rrf_merge(
            [bm25_results, chroma_results],
            top_k=5,
            rank_constant=60,
        )

        search_results = {
            "bm25": bm25_results[:5],
            "chroma": chroma_results[:5],
            "mmr": mmr_results,
            "hybrid": hybrid_results,
        }

        # 7. 질문별 평가 결과 기록
        # 정답 값(expected_value)이 없는 질문은 값 지표를 None으로 두고 요약에서 뺀다.
        expected_value = item.get("expected_value")
        has_value = expected_value is not None

        for method, results in search_results.items():
            evaluations[method].append({
                "id": item["id"],
                "question": question,
                "answer_type": item["answer_type"],
                "category": item["category"],
                "gold_sources": gold_sources,
                "hit_at_3": is_hit(results, gold_sources, 3),
                "hit_at_5": is_hit(results, gold_sources, 5),
                "value_hit_at_3": (
                    is_value_hit(results, expected_value, 3, chunk_texts)
                    if has_value else None
                ),
                "value_hit_at_5": (
                    is_value_hit(results, expected_value, 5, chunk_texts)
                    if has_value else None
                ),
                "page_value_hit_at_3": (
                    is_page_value_hit(
                        results, gold_sources, expected_value, 3, chunk_texts
                    )
                    if has_value else None
                ),
                "page_value_hit_at_5": (
                    is_page_value_hit(
                        results, gold_sources, expected_value, 5, chunk_texts
                    )
                    if has_value else None
                ),
                "results": [
                    {
                        "rank": result["rank"],
                        "chunk_id": result["chunk_id"],
                        "source_file": result["source_file"],
                        "pdf_page": result["pdf_page"],
                    }
                    for result in results
                ],
            })

        print(f"[{index:02d}/{len(questions)}] {item['id']} 평가 완료")

    # 8. 전체 Hit@K 계산
    summary = {}

    for method in methods:
        items = evaluations[method]

        summary[method] = {
            "hit_at_3": calculate_hit_rate(items, 3),
            "hit_at_5": calculate_hit_rate(items, 5),
            "hits_at_3": sum(x["hit_at_3"] for x in items),
            "hits_at_5": sum(x["hit_at_5"] for x in items),
            "total": len(items),
        }
        # 값 지표: 쪽+값(대표 지표), 값. 정답 값이 있는 질문만 센다.
        summary[method].update(summarize_value_hits(items))

    return {
        "config": {
            "embedding_model": "nlpai-lab/KURE-v1",
            "questions": len(questions),
            "chunks": len(documents),
            "bm25_candidate_k": 10,
            "chroma_candidate_k": 10,
            "mmr_fetch_k": 20,
            "mmr_lambda_mult": 0.5,
            "rrf_rank_constant": 60,
            "collection_name": target.collection_name,
        },
        "summary": summary,
        "details": evaluations,
    }


def main():
    parser = argparse.ArgumentParser(
        description="BM25·Chroma·MMR·Hybrid Hit@K 평가"
    )

    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=None,
        help="평가셋 JSON 경로 (생략 시 검색 대상에 따라 자동 결정)",
    )
    parser.add_argument(
        "--chroma-path", type=Path, default=DEFAULT_CHROMA_PATH
    )
    parser.add_argument(
        "--output-path", type=Path, default=None
    )
    parser.add_argument("--corp-code", required=True)
    parser.add_argument("--fiscal-year", required=True, type=int)
    parser.add_argument(
        "--statement-scope",
        required=True,
        choices=["consolidated", "separate"],
    )
    parser.add_argument("--rcept-no", required=True)

    args = parser.parse_args()

    # 평가 대상 생성
    target = SearchTarget(
        corp_code=args.corp_code,
        fiscal_year=args.fiscal_year,
        statement_scope=args.statement_scope,
        rcept_no=args.rcept_no,
    )

    # 평가셋은 corp/year/scope 폴더 아래의 {rcept_no}.json을 기본값으로 사용한다.
    default_dataset_path = target.dataset_path(PROJECT_ROOT)

    dataset_path = args.dataset_path or default_dataset_path

    # 평가셋 존재 여부 확인
    if not dataset_path.is_file():
        parser.error(
            f"평가셋이 존재하지 않습니다: {dataset_path}\n"
            "해당 검색 대상의 평가셋을 생성하거나 "
            "--dataset-path로 올바른 파일을 지정하세요."
        )

    # 평가 결과도 평가셋과 같은 식별 체계(corp/year/scope/{rcept_no}.json)를 따른다.
    default_output_path = target.result_path(PROJECT_ROOT)

    # 사용자가 경로를 지정했다면 해당 경로 우선 사용
    output_path = args.output_path or default_output_path

    baseline_path = (
        PROJECT_ROOT
        / "evaluation"
        / "results"
        / "retrieval_baseline.json"
    )

    if output_path.resolve() == baseline_path.resolve():
        parser.error(
            "기존 retrieval_baseline.json은 덮어쓸 수 없습니다."
        )

    # 평가 실행
    report = evaluate_retrievers(
        dataset_path=dataset_path,
        chunks_path=target.chunks_path(PROJECT_ROOT),
        chroma_path=args.chroma_path,
        target=target,
    )

    # 9. 결과 출력
    # 대표 지표는 쪽+값이고, 쪽과 값은 원인 분석용으로 함께 보여 준다.
    def percent(value):
        return "      -" if value is None else f"{value * 100:>6.1f}%"

    print("\n=== 검색 방식별 평가 결과 (Hit@3 / Hit@5) ===")
    print(
        f"{'방식':<10} {'쪽+값@3':>8} {'쪽+값@5':>8} | "
        f"{'쪽@3':>7} {'쪽@5':>7} | {'값@3':>7} {'값@5':>7}"
    )

    for method, result in report["summary"].items():
        print(
            f"{method:<10} "
            f"{percent(result.get('page_value_hit_at_3'))} "
            f"{percent(result.get('page_value_hit_at_5'))} | "
            f"{percent(result['hit_at_3'])} {percent(result['hit_at_5'])} | "
            f"{percent(result.get('value_hit_at_3'))} "
            f"{percent(result.get('value_hit_at_5'))}"
        )

    # 10. 결과 JSON 저장
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    print(f"\n평가 결과 저장: {output_path}")

if __name__ == "__main__":
    main()
