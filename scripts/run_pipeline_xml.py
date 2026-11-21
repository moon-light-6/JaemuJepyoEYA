"""DART 감사보고서 XML을 문서화·청킹·검색 평가까지 실행한다.

PDF 파이프라인과 별도로 XML 원본의 ``source_file + source_xpath``를
정답 위치로 사용한다. ``--make-draft-dataset``은 OpenDART 값과 XML 후보를
연결할 뿐이며, 만들어진 문항은 반드시 사람이 검토해 verified로 바꿔야 한다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evaluation.retrieval_metrics import contains_expected_value
from evaluation.xml_draft_dataset import build_xml_draft_dataset, detect_statement_unit, save_xml_draft_dataset
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


def load_questions(dataset_path: Path, v2_path: Path | None) -> list[dict[str, Any]]:
    """v1 평가셋의 verified 문항에, v2_path가 있으면 v2의 verified 문항을 합친다."""

    questions = load_xml_eval_dataset(dataset_path)
    if v2_path is None:
        return questions
    if not v2_path.exists():
        raise SystemExit(f"v2 평가셋이 없습니다. --make-draft-dataset-v2로 먼저 만드세요: {v2_path}")
    try:
        extra = load_xml_eval_dataset(v2_path)
    except ValueError as error:
        print(f"v2는 합치지 않습니다: {error}")
        return questions
    duplicated = {item["id"] for item in questions} & {item["id"] for item in extra}
    if duplicated:
        raise SystemExit(f"v1과 v2의 문항 ID가 겹칩니다: {sorted(duplicated)}")
    print(f"평가 문항: v1 {len(questions)}개 + v2 {len(extra)}개")
    return questions + extra


def evaluate_xml(questions: list[dict[str, Any]], chunks_path: Path, chroma_dir: Path, target: SearchTarget) -> dict[str, Any]:
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


def make_draft_dataset_v2(args: argparse.Namespace, target: SearchTarget, documents: list[dict[str, Any]], dataset_path: Path) -> int:
    """v1에 없는 질문만 draft로 모아 v2 평가셋을 만든다. v1 파일은 수정하지 않는다."""

    v2_path = target.xml_dataset_v2_path(PROJECT_ROOT)
    if v2_path.exists():
        print(f"기존 v2 평가셋을 보호하기 위해 덮어쓰지 않았습니다: {v2_path}")
        return 0
    if not dataset_path.exists():
        raise SystemExit(f"v1 평가셋이 먼저 필요합니다: {dataset_path}")
    v1_items = json.loads(dataset_path.read_text(encoding="utf-8"))
    known_questions = {item["question"] for item in v1_items}
    numbers = [int(match.group(1)) for item in v1_items if (match := re.fullmatch(r"X(\d+)", str(item.get("id", ""))))]
    accounts = fetch_financial_accounts(DartClient.from_environment(), args.corp_code, args.fiscal_year, args.statement_scope)
    unit = detect_statement_unit(documents)
    v1_unit = v1_items[0].get("unit") if v1_items else None
    print(f"v2 단위 판정: {unit} (v1 기록: {v1_unit})")
    items, skipped = build_xml_draft_dataset(accounts, documents, args.fiscal_year, args.statement_scope, unit=unit)
    new_items = [item for item in items if item["question"] not in known_questions]
    if not new_items:
        print("v1에 없는 새 문항이 없어 v2를 만들지 않았습니다.")
        for reason in skipped:
            print(f"  제외: {reason}")
        return 0
    for number, item in enumerate(new_items, start=max(numbers, default=0) + 1):
        item["id"] = f"X{number:02d}"  # v1과 합쳐도 ID가 겹치지 않도록 v1 다음 번호부터
    save_xml_draft_dataset(v2_path, new_items)
    print(f"draft XML 평가셋 v2: {v2_path} ({len(new_items)}문항)")
    for reason in skipped:
        print(f"  제외: {reason}")
    print("검증 후 annotation_status를 verified로 바꾸고, 평가는 --include-v2로 실행하세요.")
    return 0


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
    parser.add_argument("--make-draft-dataset-v2", action="store_true", help="v1에 없는 문항만 모아 {rcept_no}_retrieval_v2.json을 draft로 생성")
    parser.add_argument("--include-v2", action="store_true", help="검색 평가에 v1과 v2의 verified 문항을 함께 사용")
    parser.add_argument("--rebuild-index", action="store_true", help="해당 XML Chroma 컬렉션을 삭제 후 재색인")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.prepare_only and (args.make_draft_dataset or args.make_draft_dataset_v2):
        raise SystemExit("--prepare-only와 --make-draft-dataset(-v2)은 함께 사용할 수 없습니다.")
    if args.make_draft_dataset and args.make_draft_dataset_v2:
        raise SystemExit("--make-draft-dataset과 --make-draft-dataset-v2는 함께 사용할 수 없습니다.")
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

    if args.make_draft_dataset_v2:
        return make_draft_dataset_v2(args, target, documents, dataset_path)

    try:
        questions = load_questions(dataset_path, target.xml_dataset_v2_path(PROJECT_ROOT) if args.include_v2 else None)
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
    report = evaluate_xml(questions, target.xml_chunks_path(PROJECT_ROOT), args.chroma_dir, target)
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
