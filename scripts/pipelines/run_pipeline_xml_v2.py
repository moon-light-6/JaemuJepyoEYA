"""XML v2 청크를 검증된 문항으로 평가한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evaluation.xml_draft_dataset import build_xml_draft_dataset, detect_statement_unit, save_xml_draft_dataset
from fin_research.ingestion.auto_resolve import (
    choose_report,
    fetch_financial_accounts,
    find_corp,
    load_corp_codes,
    normalize_corp_name,
)
from fin_research.ingestion.dart_client import DartClient
from fin_research.ingestion.industry_codes import industry_division_name
from fin_research.ingestion.xml_documents import build_xml_documents
from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.xml_retrieval import build_xml_chroma, load_xml_eval_dataset
from scripts.processing.build_xml_chunks_v2 import build_xml_chunks
from scripts.pipelines.run_pipeline_xml import evaluate_xml, load_questions, make_draft_dataset_v2, write_json


CORP_CODE_CACHE = PROJECT_ROOT / "data" / "raw" / "dart" / "CORPCODE.xml"




def select_company(query, corps, client):
    """동명 후보 전체를 표시하고 사용자에게 번호 선택을 요청한다."""
    if re.fullmatch(r"\d{8}", query) or (re.fullmatch(r"[0-9A-Z]{6}", query.upper()) and any(c.isdigit() for c in query)):
        return find_corp(query, corps)
    matches = [corp for corp in corps if corp.corp_name == query]
    if not matches:
        key = normalize_corp_name(query)
        matches = [corp for corp in corps if normalize_corp_name(corp.corp_name) == key]
    if not matches:
        raise ValueError(f"'{query}'에 해당하는 기업이 없습니다.")
    if len(matches) == 1:
        return matches[0]
    print(f"'{query}'와 일치하는 기업이 {len(matches)}개입니다. 번호로 선택하세요.")
    for index, corp in enumerate(matches, start=1):
        info = client._get_json("https://opendart.fss.or.kr/api/company.json", {"crtfc_key": client.api_key, "corp_code": corp.corp_code})
        code = str(info.get("induty_code") or "").strip()
        name = industry_division_name(code)
        print(f"  {index}. {corp.corp_name} | 고유번호: {corp.corp_code} | 종목코드: {corp.stock_code or '-'} | 대표자: {info.get('ceo_nm') or '확인 불가'} | 업종 중분류: {name} | 업종코드: {code or '확인 불가'}")
    while True:
        try:
            selected = input(f"선택할 번호 (1~{len(matches)}, 취소: Enter): ").strip()
        except EOFError:
            raise ValueError("기업 선택이 필요합니다. --corp-code에 고유번호를 지정해 다시 실행하세요.") from None
        if not selected:
            raise ValueError("기업 선택이 취소되었습니다.")
        if selected.isascii() and selected.isdigit() and 1 <= int(selected) <= len(matches):
            return matches[int(selected) - 1]
        print(f"1부터 {len(matches)}까지의 번호를 입력하세요.")



def resolve_target_from_company(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str | None:
    """기업을 확정하고 필요한 사업보고서 접수번호를 조회한다."""
    if not args.company and not args.corp_code:
        parser.error("--company 또는 --corp-code를 지정해야 합니다.")
    if args.corp_code and args.rcept_no and not args.company:
        return None
    client = DartClient.from_environment()
    corps = load_corp_codes(client, CORP_CODE_CACHE, refresh=args.refresh_corp_codes)
    corp = select_company(args.corp_code or args.company.strip(), corps, client)
    if args.company and args.corp_code and normalize_corp_name(args.company) != normalize_corp_name(corp.corp_name):
        parser.error(f"기업명 {args.company}과 고유번호 {args.corp_code}의 기업 {corp.corp_name}이 다릅니다.")
    candidates = client.search_business_reports(corp.corp_code, args.fiscal_year, f"{args.fiscal_year + 1}-01-01", f"{args.fiscal_year + 1}-12-31")
    if not candidates:
        raise ValueError(
            f"요청하신 {corp.corp_name} (고유번호 {corp.corp_code})의 "
            f"{args.fiscal_year}년 사업보고서가 조회되지 않았습니다. "
            f"조회 범위: {args.fiscal_year + 1}년 공시 목록. "
            "아직 공시되지 않았거나 해당 기간에 사업보고서가 없을 수 있습니다."
        )
    report, reason = choose_report(candidates, args.rcept_no, args.report_choice)
    args.corp_code, args.rcept_no, args.company = corp.corp_code, report.rcept_no, corp.corp_name
    print(f"OpenDART 식별자: {corp.corp_name} / corp_code={corp.corp_code} / rcept_no={report.rcept_no} ({reason})", flush=True)
    return corp.corp_name


def main() -> None:
    parser = argparse.ArgumentParser(description="XML v2 청크 검색 평가")
    parser.add_argument("--corp-code", help="OpenDART 고유번호 8자리 (기업명을 쓰면 자동 조회)")
    parser.add_argument("--fiscal-year", required=True, type=int)
    parser.add_argument("--statement-scope", required=True, choices=("consolidated", "separate"))
    parser.add_argument("--rcept-no", help="사업보고서 접수번호 (기업명을 쓰면 자동 조회)")
    parser.add_argument("--company", help="기업명·6자리 종목코드·8자리 고유번호. 지정하면 식별자를 자동 조회")
    parser.add_argument("--report-choice", choices=("latest", "original"), default="latest",
                        help="정정공시가 여럿일 때 최신본(기본) 또는 최초본 선택")
    parser.add_argument("--refresh-corp-codes", action="store_true", help="OpenDART 고유번호 캐시를 새로 받음")
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

    resolved_company = resolve_target_from_company(args, parser)

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
                print(f"OpenDART XML 원본 ZIP 수집: {target.rcept_no}", flush=True)
                DartClient.from_environment().download_original_zip(target.rcept_no, zip_path)
            company = resolved_company or args.company
            if company is None:
                client = DartClient.from_environment()
                company = find_corp(
                    target.corp_code, load_corp_codes(client, CORP_CODE_CACHE, refresh=args.refresh_corp_codes)
                ).corp_name
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
    try:
        main()
    except Exception as error:
        print(f"실패: {error}", file=sys.stderr)
        sys.exit(1)
