"""네 가지 검색기의 Hit@3·Hit@5 성능 평가 실행 스크립트."""

import argparse
import json
import sys
from pathlib import Path


# 1. 프로젝트 루트 및 모듈 경로
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evaluation.retrieval_metrics import (
    load_eval_dataset,
    is_hit,
    calculate_hit_rate,
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


# 2. 기본 경로
DEFAULT_DATASET_PATH = (
    PROJECT_ROOT / "evaluation" / "dataset" / "eval_retrieval.json"
)

DEFAULT_CHUNKS_PATH = (
    PROJECT_ROOT / "data" / "processed" / "dart"
    / "00126380" / "2025" / "20260310002820_chunks.json"
)

DEFAULT_CHROMA_PATH = PROJECT_ROOT / "data" / "chroma_samsung"

DEFAULT_OUTPUT_PATH = (
    PROJECT_ROOT / "evaluation" / "results" / "retrieval_baseline.json"
)


def evaluate_retrievers(
    dataset_path: Path,
    chunks_path: Path,
    chroma_path: Path,
) -> dict:
    """동일 평가셋으로 네 검색 방식을 비교한다."""

    questions = load_eval_dataset(dataset_path)
    documents = load_chunk_documents(chunks_path)

    # BM25는 평가 전체에서 한 번만 구성
    bm25 = build_bm25_retriever(documents, k=10)

    # Chroma와 임베딩 모델도 한 번만 로드
    vectorstore = create_chroma(chroma_path)

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
        for method, results in search_results.items():
            evaluations[method].append({
                "id": item["id"],
                "question": question,
                "answer_type": item["answer_type"],
                "category": item["category"],
                "gold_sources": gold_sources,
                "hit_at_3": is_hit(results, gold_sources, 3),
                "hit_at_5": is_hit(results, gold_sources, 5),
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
        },
        "summary": summary,
        "details": evaluations,
    }


def main():
    parser = argparse.ArgumentParser(
        description="BM25·Chroma·MMR·Hybrid Hit@K 평가"
    )

    parser.add_argument(
        "--dataset-path", type=Path, default=DEFAULT_DATASET_PATH
    )
    parser.add_argument(
        "--chunks-path", type=Path, default=DEFAULT_CHUNKS_PATH
    )
    parser.add_argument(
        "--chroma-path", type=Path, default=DEFAULT_CHROMA_PATH
    )
    parser.add_argument(
        "--output-path", type=Path, default=DEFAULT_OUTPUT_PATH
    )

    args = parser.parse_args()

    report = evaluate_retrievers(
        dataset_path=args.dataset_path,
        chunks_path=args.chunks_path,
        chroma_path=args.chroma_path,
    )

    # 9. 결과 출력
    print("\n=== 검색 방식별 평가 결과 ===")
    print(f"{'방식':<12} {'Hit@3':>10} {'Hit@5':>10}")

    for method, result in report["summary"].items():
        print(
            f"{method:<12} "
            f"{result['hit_at_3'] * 100:>9.1f}% "
            f"{result['hit_at_5'] * 100:>9.1f}%"
        )

    # 10. 결과 JSON 저장
    args.output_path.parent.mkdir(parents=True, exist_ok=True)

    with args.output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    print(f"\n평가 결과 저장: {args.output_path}")


if __name__ == "__main__":
    main()
