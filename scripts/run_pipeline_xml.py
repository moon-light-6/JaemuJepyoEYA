"""DART 감사보고서 XML을 문서화·청킹·검색 평가까지 실행한다.

PDF 파이프라인과 별도로 XML 원본의 ``source_file + source_xpath``를
정답 위치로 사용한다. ``--make-draft-dataset``은 OpenDART 값과 XML 후보를
연결할 뿐이며, 만들어진 문항은 반드시 사람이 검토해 verified로 바꿔야 한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evaluation.retrieval_metrics import contains_expected_value
from evaluation.xml_draft_dataset import build_xml_draft_dataset, save_xml_draft_dataset
from fin_research.ingestion.auto_resolve import fetch_financial_accounts
from fin_research.ingestion.dart_client import DartClient
from fin_research.ingestion.xml_documents import build_xml_documents
from fin_research.retrieval.bm25_retriever import build_bm25_retriever
from fin_research.retrieval.chroma_retriever import create_chroma
from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.xml_retrieval import (
    build_xml_chroma,
    is_xml_hit,
    is_xml_source_value_hit,
    load_xml_chunk_documents,
    load_xml_eval_dataset,
    result_from_document,
)
from scripts.build_xml_chunks import build_xml_chunks


DEFAULT_CHROMA_DIR = PROJECT_ROOT / "data" / "chroma_multi"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rrf_merge(result_lists: list[list[dict[str, Any]]], top_k: int = 5) -> list[dict[str, Any]]:
    """PDF 결과 형식에 의존하지 않는 XML 전용 RRF 결합."""

    scores: dict[str, float] = {}
    records: dict[str, dict[str, Any]] = {}
    for results in result_lists:
        for rank, result in enumerate(results, start=1):
            chunk_id = str(result["chunk_id"])
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (60 + rank)
            records.setdefault(chunk_id, result)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:top_k]
    return [{**records[chunk_id], "rank": index, "rrf_score": scores[chunk_id]}
            for index, chunk_id in enumerate(ordered, start=1)]


def evaluate_xml(dataset_path: Path, chunks_path: Path, chroma_dir: Path, target: SearchTarget) -> dict[str, Any]:
    questions = load_xml_eval_dataset(dataset_path)
    documents = load_xml_chunk_documents(chunks_path)
    bm25 = build_bm25_retriever(documents, k=10)
    vectorstore = create_chroma(chroma_dir, target.xml_collection_name)
    if vectorstore._collection.count() != len(documents):
        raise ValueError("XML Chroma 청크 수가 입력 JSON과 다릅니다. --rebuild-index를 사용하세요.")

    methods = ("bm25", "chroma", "mmr", "hybrid")
    details: dict[str, list[dict[str, Any]]] = {method: [] for method in methods}
    for item in questions:
        query = item["question"]
        bm25_results = [result_from_document(document, rank) for rank, document in enumerate(bm25.invoke(query), start=1)]
        chroma_results = [result_from_document(document, rank, distance=float(distance)) for rank, (document, distance) in enumerate(vectorstore.similarity_search_with_score(query, k=10), start=1)]
        mmr_results = [result_from_document(document, rank) for rank, document in enumerate(vectorstore.max_marginal_relevance_search(query, k=5, fetch_k=20, lambda_mult=0.5), start=1)]
        all_results = {
            "bm25": bm25_results[:5],
            "chroma": chroma_results[:5],
            "mmr": mmr_results,
            "hybrid": rrf_merge([bm25_results, chroma_results]),
        }
        for method, results in all_results.items():
            expected = item.get("expected_value")
            details[method].append({
                "id": item["id"], "question": query, "gold_sources": item["gold_sources"],
                "hit_at_3": is_xml_hit(results, item["gold_sources"], 3),
                "hit_at_5": is_xml_hit(results, item["gold_sources"], 5),
                "value_hit_at_3": None if expected is None else any(contains_expected_value(expected, result["text"]) for result in results[:3]),
                "value_hit_at_5": None if expected is None else any(contains_expected_value(expected, result["text"]) for result in results[:5]),
                "source_value_hit_at_3": None if expected is None else is_xml_source_value_hit(results, item["gold_sources"], expected, 3, contains_expected_value),
                "source_value_hit_at_5": None if expected is None else is_xml_source_value_hit(results, item["gold_sources"], expected, 5, contains_expected_value),
                "results": [{key: result[key] for key in ("rank", "chunk_id", "source_file", "source_xpath", "title")} for result in results],
            })

    summary: dict[str, dict[str, float | int | None]] = {}
    for method, rows in details.items():
        valued = [row for row in rows if row["value_hit_at_3"] is not None]
        rate = lambda key: sum(bool(row[key]) for row in rows) / len(rows)
        value_rate = lambda key: None if not valued else sum(bool(row[key]) for row in valued) / len(valued)
        summary[method] = {
            "hit_at_3": rate("hit_at_3"), "hit_at_5": rate("hit_at_5"),
            "value_hit_at_3": value_rate("value_hit_at_3"), "value_hit_at_5": value_rate("value_hit_at_5"),
            "source_value_hit_at_3": value_rate("source_value_hit_at_3"),
            "source_value_hit_at_5": value_rate("source_value_hit_at_5"), "total": len(rows),
        }
    return {"config": {"source_format": "xml", "questions": len(questions), "chunks": len(documents), "collection_name": target.xml_collection_name}, "summary": summary, "details": details}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DART XML 청킹·검색 평가 파이프라인")
    parser.add_argument("--company", required=True)
    parser.add_argument("--corp-code", required=True)
    parser.add_argument("--fiscal-year", required=True, type=int)
    parser.add_argument("--rcept-no", required=True)
    parser.add_argument("--statement-scope", choices=("consolidated", "separate"), required=True)
    parser.add_argument("--chunk-size", type=int, default=600)
    parser.add_argument("--chunk-overlap", type=int, default=100)
    parser.add_argument("--chroma-dir", type=Path, default=DEFAULT_CHROMA_DIR)
    parser.add_argument("--prepare-only", action="store_true", help="XML 문서·청크까지만 생성")
    parser.add_argument("--make-draft-dataset", action="store_true", help="OpenDART 기반 draft 평가셋 생성")
    parser.add_argument("--rebuild-index", action="store_true", help="해당 XML Chroma 컬렉션을 삭제 후 재색인")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.prepare_only and args.make_draft_dataset:
        raise SystemExit("--prepare-only와 --make-draft-dataset은 함께 사용할 수 없습니다.")
    target = SearchTarget(args.corp_code, args.fiscal_year, args.statement_scope, args.rcept_no)
    zip_path = PROJECT_ROOT / "data" / "raw" / "dart" / args.corp_code / str(args.fiscal_year) / f"{args.rcept_no}.zip"
    documents, metadata = build_xml_documents(zip_path, args.corp_code, args.fiscal_year, args.statement_scope, args.company)
    chunks = build_xml_chunks(documents, args.chunk_size, args.chunk_overlap)
    write_json(target.xml_documents_path(PROJECT_ROOT), documents)
    write_json(target.xml_chunks_path(PROJECT_ROOT), chunks)
    write_json(target.xml_pipeline_path(PROJECT_ROOT), {**metadata, "chunk_size": args.chunk_size, "chunk_overlap": args.chunk_overlap, "documents": len(documents), "chunks": len(chunks)})
    print(f"XML 문서 / 청크: {len(documents)}개 / {len(chunks)}개")
    print(f"XML 문서: {target.xml_documents_path(PROJECT_ROOT)}")
    print(f"XML 청크: {target.xml_chunks_path(PROJECT_ROOT)}")
    if args.prepare_only:
        return 0

    dataset_path = target.xml_dataset_path(PROJECT_ROOT)
    if args.make_draft_dataset:
        if dataset_path.exists():
            print(f"기존 평가셋을 보호하기 위해 덮어쓰지 않았습니다: {dataset_path}")
            return 0
        accounts = fetch_financial_accounts(DartClient.from_environment(), args.corp_code, args.fiscal_year, args.statement_scope)
        items, skipped = build_xml_draft_dataset(accounts, documents, args.fiscal_year, args.statement_scope)
        save_xml_draft_dataset(dataset_path, items)
        print(f"draft XML 평가셋: {dataset_path} ({len(items)}문항)")
        for reason in skipped:
            print(f"  제외: {reason}")
        return 0

    try:
        questions = load_xml_eval_dataset(dataset_path)
    except ValueError as error:
        print(f"평가를 건너뜁니다: {error}")
        print(f"draft 문항을 검토해 verified로 바꾼 뒤 다시 실행하세요: {dataset_path}")
        return 0
    if args.rebuild_index:
        import chromadb
        try:
            chromadb.PersistentClient(path=str(args.chroma_dir)).delete_collection(target.xml_collection_name)
        except Exception:
            pass
    build_xml_chroma(target.xml_chunks_path(PROJECT_ROOT), args.chroma_dir, target.xml_collection_name)
    report = evaluate_xml(dataset_path, target.xml_chunks_path(PROJECT_ROOT), args.chroma_dir, target)
    write_json(target.xml_result_path(PROJECT_ROOT), report)
    print(f"평가 결과 저장: {target.xml_result_path(PROJECT_ROOT)}")
    
    # 평가 지표 출력 순서
    metrics = [
        ("위치+값@3", "source_value_hit_at_3"),
        ("위치+값@5", "source_value_hit_at_5"),
        ("위치@3", "hit_at_3"),
        ("위치@5", "hit_at_5"),
        ("값@3", "value_hit_at_3"),
        ("값@5", "value_hit_at_5"),
    ]

    methods = [
        ("BM25", "bm25"),
        ("Chroma", "chroma"),
        ("MMR", "mmr"),
        ("Hybrid-RRF", "hybrid"),
    ]

    print("\n=== 검색 평가 결과 (%) ===")
    print("방식\t" + "\t".join(name for name, _ in metrics))

    for name, key in methods:
        row = report["summary"][key]
        values = [
            "N/A" if row[metric] is None
            else f"{row[metric] * 100:.1f}%"
            for _, metric in metrics
        ]
        print(name + "\t" + "\t".join(values))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
