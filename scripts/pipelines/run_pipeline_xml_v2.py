"""XML 문서를 v2로 청킹하고 검증된 평가셋으로 검색 성능을 평가한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evaluation.xml_draft_dataset import build_xml_draft_dataset, detect_statement_unit, save_xml_draft_dataset
from fin_research.ingestion.auto_resolve import fetch_financial_accounts
from fin_research.ingestion.dart_client import DartClient
from fin_research.ingestion.xml_documents import build_xml_documents
from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.xml_retrieval import build_xml_chroma, load_xml_eval_dataset
from scripts.processing.build_xml_chunks_v2 import build_xml_chunks
from scripts.ingestion.collect import load_target
from scripts.pipelines.run_pipeline_xml import evaluate_xml, load_questions, make_draft_dataset_v2, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="XML v2 청크 검색 평가")
    parser.add_argument("--corp-code", required=True)
    parser.add_argument("--fiscal-year", required=True, type=int)
    parser.add_argument("--statement-scope", required=True, choices=("consolidated", "separate"))
    parser.add_argument("--rcept-no", required=True)
    parser.add_argument("--company", help="회사명 (생략 시 기업 설정에서 조회)")
    parser.add_argument("--chunks-path", type=Path, help="이미 생성된 청크 JSON 경로")
    parser.add_argument("--chunk-size", type=int, default=600, help="v2 청크 크기")
    parser.add_argument("--chunk-overlap", type=int, default=100, help="v2 청크 겹침")
    parser.add_argument("--dataset-path", type=Path, help="검증된 평가셋 JSON 경로")
    draft_options = parser.add_mutually_exclusive_group()
    draft_options.add_argument(
        "--make-draft-dataset",
        action="store_true",
        help="기존 평가셋이 없는 기업의 첫 draft 평가셋 생성",
    )
    draft_options.add_argument(
        "--make-draft-dataset-v2",
        action="store_true",
        help="기존 평가셋에 없는 추가 draft 문항 생성",
    )
    parser.add_argument("--baseline-path", type=Path, help="비교할 기존 평가 결과 JSON 경로")
    parser.add_argument("--output-path", type=Path, help="v2 평가 결과 JSON 경로")
    parser.add_argument("--chroma-dir", type=Path, help="v2 Chroma 인덱스 상위 폴더")
    args = parser.parse_args()

    target = SearchTarget(args.corp_code, args.fiscal_year, args.statement_scope, args.rcept_no)
    chunks_path = args.chunks_path or (
        target.xml_scope_dir(PROJECT_ROOT) / f"{target.rcept_no}_xml_chunks_v2.json"
    )
    default_dataset = target.xml_dataset_path(PROJECT_ROOT)
    v2_dataset = default_dataset.with_name(f"{target.rcept_no}_retrieval_v2.json")
    dataset_path = args.dataset_path or default_dataset
    output_path = args.output_path or target.xml_result_path(PROJECT_ROOT).with_name(
        f"{target.rcept_no}_retrieval_v2.json"
    )
    chroma_root = args.chroma_dir or PROJECT_ROOT / "data" / "chroma_xml_v2"

    make_draft = args.make_draft_dataset or args.make_draft_dataset_v2
    if make_draft:
        if args.dataset_path is not None or args.chunks_path is not None:
            parser.error("추가 draft 생성에는 --dataset-path와 --chunks-path를 사용할 수 없습니다.")
        if args.make_draft_dataset_v2 and not default_dataset.is_file():
            raise SystemExit(f"기존 평가셋이 먼저 필요합니다: {default_dataset}")
    else:
        if args.baseline_path is not None and not args.baseline_path.is_file():
            raise SystemExit(f"기존 평가 결과 파일을 찾지 못했습니다: {args.baseline_path}")
        if args.dataset_path is None and v2_dataset.exists():
            questions = load_questions(dataset_path, v2_dataset)
            print(f"추가 평가셋: {v2_dataset}", flush=True)
        else:
            questions = load_xml_eval_dataset(dataset_path)

    if args.chunks_path is None or make_draft:
        documents_path = target.xml_documents_path(PROJECT_ROOT)
        if not documents_path.exists():
            zip_path = (
                PROJECT_ROOT / "data" / "raw" / "dart" / target.corp_code
                / str(target.fiscal_year) / f"{target.rcept_no}.zip"
            )
            if not zip_path.is_file():
                raise SystemExit(
                    f"원본 ZIP을 찾지 못했습니다: {zip_path}\n"
                    f"먼저 python scripts/ingestion/collect.py --corp-code {target.corp_code} "
                    f"--fiscal-year {target.fiscal_year} 명령으로 수집하세요."
                )
            company = args.company
            if company is None:
                company, _ = load_target(target.corp_code, target.fiscal_year)
                company = company["name"]
            print(f"원본 ZIP에서 XML 문서 생성: {zip_path}", flush=True)
            documents, _ = build_xml_documents(
                zip_path, target.corp_code, target.fiscal_year,
                target.statement_scope, company,
            )
            write_json(documents_path, documents)
            print(f"XML 문서 저장: {documents_path}", flush=True)
        documents = json.loads(documents_path.read_text(encoding="utf-8"))
        chunks = build_xml_chunks(documents, args.chunk_size, args.chunk_overlap)
        write_json(chunks_path, chunks)
        print(f"v2 청크 생성: {len(chunks)}개 ({args.chunk_size}/{args.chunk_overlap})", flush=True)
        if args.make_draft_dataset:
            if default_dataset.exists():
                print(f"기존 평가셋을 보호하기 위해 덮어쓰지 않았습니다: {default_dataset}")
                return
            accounts = fetch_financial_accounts(
                DartClient.from_environment(), target.corp_code,
                target.fiscal_year, target.statement_scope,
            )
            receipts = {row.get("rcept_no") for row in accounts}
            if accounts and receipts != {target.rcept_no}:
                raise SystemExit(
                    f"OpenDART 계정의 접수번호 {sorted(str(item) for item in receipts)}가 "
                    f"요청한 접수번호 {target.rcept_no}와 다릅니다."
                )
            unit = detect_statement_unit(documents)
            items, skipped = build_xml_draft_dataset(
                accounts, documents, target.fiscal_year, target.statement_scope,
                unit=unit,
            )
            save_xml_draft_dataset(default_dataset, items)
            print(f"재무제표 단위 판정: {unit}")
            print(f"draft XML 평가셋: {default_dataset} ({len(items)}문항)")
            for reason in skipped:
                print(f"  제외: {reason}")
            print("문항을 검증한 뒤 이 스크립트를 옵션 없이 실행해 평가하세요.")
            return
        if args.make_draft_dataset_v2:
            make_draft_dataset_v2(args, target, documents, default_dataset)
            print("추가 문항을 검증한 뒤 이 스크립트를 옵션 없이 실행해 평가하세요.")
            return

    # 청크 내용이 달라지면 새 저장소를 사용해 이전 임베딩의 재사용을 막는다.
    digest = hashlib.sha256(chunks_path.read_bytes()).hexdigest()
    chroma_dir = chroma_root / digest

    print(f"v2 청크: {chunks_path}", flush=True)
    print(f"평가셋: {dataset_path}", flush=True)
    print(f"검증된 평가 문항: {len(questions)}개", flush=True)
    print(f"Chroma 저장소: {chroma_dir}", flush=True)
    print("청크 색인 중...", flush=True)
    count = build_xml_chroma(chunks_path, chroma_dir, target.xml_collection_name)
    print(f"색인된 청크: {count}개", flush=True)

    print("검색 평가 중...", flush=True)
    report = evaluate_xml(questions, chunks_path, chroma_dir, target)
    report["config"].update({
        "chunks_path": str(chunks_path),
        "chunks_sha256": digest,
        "dataset_path": str(dataset_path),
        "chroma_dir": str(chroma_dir),
    })
    if args.chunks_path is None:
        report["config"].update({
            "chunk_size": args.chunk_size,
            "chunk_overlap": args.chunk_overlap,
        })
    write_json(output_path, report)

    print("\n=== 검색 평가 결과 (%) ===")
    metrics = (
        ("위치+값@3", "source_value_hit_at_3"),
        ("위치+값@5", "source_value_hit_at_5"),
        ("위치@3", "hit_at_3"),
        ("위치@5", "hit_at_5"),
        ("값@3", "value_hit_at_3"),
        ("값@5", "value_hit_at_5"),
    )
    print("방식\t" + "\t".join(label for label, _ in metrics))
    for name, key in (("BM25", "bm25"), ("Chroma", "chroma"), ("MMR", "mmr"), ("Hybrid-RRF", "hybrid")):
        row = report["summary"][key]
        values = [
            "N/A" if row[metric] is None else f"{row[metric] * 100:.1f}%"
            for _, metric in metrics
        ]
        print(name + "\t" + "\t".join(values))
    print(f"평가 결과 저장: {output_path}")

    if args.baseline_path:
        baseline = json.loads(args.baseline_path.read_text(encoding="utf-8"))
        print("\n=== 기존 결과와 비교 (위치+값@5) ===")
        for method in ("bm25", "chroma", "mmr", "hybrid"):
            before = baseline["details"][method]
            after = report["details"][method]
            before_questions = {row["id"]: (row["question"], row["gold_sources"]) for row in before}
            after_questions = {row["id"]: (row["question"], row["gold_sources"]) for row in after}
            if before_questions != after_questions:
                print(f"{method}: 평가 문항 또는 정답 출처가 달라 비교를 건너뜁니다.")
                continue
            before_rate = baseline["summary"][method]["source_value_hit_at_5"]
            after_rate = report["summary"][method]["source_value_hit_at_5"]
            if before_rate is None or after_rate is None:
                print(f"{method}: 비교할 금액 정답이 없습니다.")
                continue
            print(f"{method}: {before_rate * 100:.1f}% → {after_rate * 100:.1f}%")


if __name__ == "__main__":
    main()
