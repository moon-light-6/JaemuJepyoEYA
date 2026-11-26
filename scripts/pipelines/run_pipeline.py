"""기업명·종목코드만 넣으면 공식 PDF 수집부터 Hit@3·Hit@5 평가까지 한 번에 실행한다.

사람이 매번 넣던 값을 자동으로 채운다.
  corp_code  : OpenDART 고유번호 파일에서 기업명·종목코드로 조회
  rcept_no   : OpenDART 공시목록 (정정공시가 있으면 최신 접수본)
  dcm_no     : DART 공시 뷰어 페이지에서 추출
  페이지 범위 : PDF의 '2. 연결재무제표 / 3. 연결재무제표 주석 / 4. 재무제표' 제목으로 판정
  평가셋     : OpenDART 재무제표 API 금액이 적힌 PDF 페이지를 Gold page로 자동 생성
companies.yaml에 rcept_no, official_pdf.dcm_no가 있으면 그 값을 우선한다.

예)
  python scripts/pipelines/run_pipeline.py --companies 파마리서치
  python scripts/pipelines/run_pipeline.py --companies 파마리서치 HLB 214150
  python scripts/pipelines/run_pipeline.py --from-config
  python scripts/pipelines/run_pipeline.py --companies 삼성전자 HLB 에코프로 --resolve-only
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evaluation.auto_dataset import ANNOTATION_METHOD, build_auto_eval_set
from fin_research.ingestion.auto_resolve import (
    CorpInfo,
    choose_report,
    fetch_dcm_no,
    fetch_financial_accounts,
    find_corp,
    load_corp_codes,
)
from fin_research.ingestion.dart_client import DartClient, DartError
from fin_research.ingestion.pdf_parser import (
    PdfDocumentError,
    dart_pdf_download_page_url,
    dart_pdf_url,
    download_official_pdf,
    inspect_pdf,
    save_pdf_inspection,
    sha256_file,
)
from fin_research.ingestion.section_detector import SectionDetectionError, detect_sections
from fin_research.retrieval.search_target import SearchTarget


CONFIG_PATH = PROJECT_ROOT / "configs" / "companies.yaml"
CORP_CODE_CACHE = PROJECT_ROOT / "data" / "raw" / "dart" / "CORPCODE.xml"
DEFAULT_CHROMA_DIR = PROJECT_ROOT / "data" / "chroma_multi"
DEFAULT_SUMMARY_PATH = PROJECT_ROOT / "evaluation" / "results" / "retrieval_summary.csv"
METHODS = ["bm25", "chroma", "mmr", "hybrid"]

SUMMARY_FIELDS = [
    "company", "corp_code", "stock_code", "fiscal_year", "statement_scope",
    "rcept_no", "dcm_no", "statements_pages", "notes_pages", "chunks",
    "questions", "dataset",
    *[f"{method}_hit{k}" for method in METHODS for k in (3, 5)],
    "status", "message", "updated_at",
]


class StepError(Exception):
    """파이프라인 한 단계가 실패한 경우."""


# ---------------------------------------------------------------------------
# 설정·실행 도우미
# ---------------------------------------------------------------------------

def load_config() -> list[dict]:
    if not CONFIG_PATH.is_file():
        return []
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return list(config.get("companies", []))


def find_config_entry(entries: list[dict], query: str, corp_code: str | None = None) -> dict | None:
    for entry in entries:
        if corp_code and entry.get("corp_code") == corp_code:
            return entry
        if entry.get("name") == query or entry.get("corp_code") == query:
            return entry
    return None


def run_step(label: str, script: str, *arguments: object) -> None:
    command = [sys.executable, str(PROJECT_ROOT / script), *map(str, arguments)]
    shown = " ".join(f'"{part}"' if " " in part else part for part in map(str, arguments))
    print(f"\n  $ python {script} {shown}", flush=True)
    environment = dict(os.environ, PYTHONUTF8="1")
    result = subprocess.run(command, cwd=PROJECT_ROOT, env=environment)
    if result.returncode != 0:
        raise StepError(f"{label} 단계 실패 (종료 코드 {result.returncode})")


def log(company: str, message: str) -> None:
    print(f"[{company}] {message}", flush=True)


def page_range_text(page_range) -> str:
    return f"{page_range.start_page}-{page_range.end_page}"


# ---------------------------------------------------------------------------
# 단계별 처리
# ---------------------------------------------------------------------------

class Pipeline:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = DartClient.from_environment()
        self.config = load_config()
        self._corps: list[CorpInfo] | None = None

    @property
    def corps(self) -> list[CorpInfo]:
        if self._corps is None:
            self._corps = load_corp_codes(
                self.client, CORP_CODE_CACHE, refresh=self.args.refresh_corp_codes
            )
        return self._corps

    # 1. 기업 확인 -----------------------------------------------------------
    def resolve_corp(self, query: str) -> tuple[CorpInfo, dict | None]:
        entry = find_config_entry(self.config, query)
        if entry and entry.get("corp_code"):
            corp_code = entry["corp_code"]
            try:
                corp = next(c for c in self.corps if c.corp_code == corp_code)
            except StopIteration:
                corp = CorpInfo(corp_code, entry.get("name", query), "")
            return corp, entry
        corp = find_corp(query, self.corps)
        return corp, find_config_entry(self.config, query, corp.corp_code)

    # 2~3. 접수번호·문서번호 --------------------------------------------------
    def resolve_report(self, corp: CorpInfo, entry: dict | None, use_config: bool = True):
        year = self.args.fiscal_year
        report_config = ((entry or {}).get("reports") or {}).get(str(year)) or {}
        candidates = self.client.search_business_reports(
            corp_code=corp.corp_code,
            fiscal_year=year,
            disclosure_start=report_config.get("disclosure_start", f"{year + 1}-01-01"),
            disclosure_end=report_config.get("disclosure_end", f"{year + 1}-12-31"),
        )
        configured = report_config.get("rcept_no") if use_config else None
        report, reason = choose_report(candidates, configured, self.args.report_choice)

        configured_dcm = (report_config.get("official_pdf") or {}).get("dcm_no")
        metadata_path = self.raw_dir(corp) / f"{report.rcept_no}_pdf.json"
        if use_config and configured_dcm and report_config.get("rcept_no") == report.rcept_no:
            return report, reason, str(configured_dcm), "companies.yaml", report_config
        if use_config and metadata_path.is_file():
            saved = json.loads(metadata_path.read_text(encoding="utf-8")).get("dcm_no")
            if saved:
                return report, reason, str(saved), "기존 PDF 메타데이터", report_config
        return report, reason, fetch_dcm_no(report.rcept_no), "DART 뷰어", report_config

    def raw_dir(self, corp: CorpInfo) -> Path:
        return PROJECT_ROOT / "data" / "raw" / "dart" / corp.corp_code / str(self.args.fiscal_year)

    def processed_dir(self, corp: CorpInfo) -> Path:
        return PROJECT_ROOT / "data" / "processed" / "dart" / corp.corp_code / str(self.args.fiscal_year)

    # 4. 공식 PDF -------------------------------------------------------------
    def collect_pdf(self, corp: CorpInfo, name: str, rcept_no: str, dcm_no: str) -> Path:
        raw_dir, processed_dir = self.raw_dir(corp), self.processed_dir(corp)
        pdf_path = raw_dir / f"{rcept_no}.pdf"
        metadata_path = raw_dir / f"{rcept_no}_pdf.json"
        inspection_path = processed_dir / f"{rcept_no}_pdf_pages.json"
        source_url = dart_pdf_url(rcept_no, dcm_no)

        metadata = None
        if pdf_path.is_file() and metadata_path.is_file():
            saved = json.loads(metadata_path.read_text(encoding="utf-8"))
            if saved.get("source_url") == source_url and saved.get("sha256") == sha256_file(pdf_path):
                metadata = saved
        if metadata is None:
            if pdf_path.exists() or metadata_path.exists():
                raise PdfDocumentError(
                    "기존 PDF 또는 메타데이터의 URL·해시가 일치하지 않습니다. 원본을 덮어쓰지 않았습니다: "
                    f"{pdf_path}"
                )
            log(name, f"공식 PDF 다운로드: {source_url}")
            metadata = download_official_pdf(
                source_url, pdf_path, referer_url=dart_pdf_download_page_url(rcept_no, dcm_no)
            )
            metadata.update({
                "company_name": name,
                "company_code": corp.corp_code,
                "fiscal_year": self.args.fiscal_year,
                "rcept_no": rcept_no,
                "dcm_no": dcm_no,
                "report_type": "business",
            })
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            log(name, "기존 공식 PDF 재사용")

        if self.args.rebuild or not inspection_path.is_file():
            save_pdf_inspection(inspect_pdf(pdf_path, metadata), inspection_path)
        return inspection_path

    # (선택) XML 재무제표 분석 ------------------------------------------------
    def run_xml(self, corp: CorpInfo, name: str, report) -> None:
        zip_path = self.raw_dir(corp) / f"{report.rcept_no}.zip"
        self.client.download_original_zip(report.rcept_no, zip_path)
        self.client.save_metadata(report, zip_path)
        base = self.processed_dir(corp) / report.rcept_no
        structure = f"{base}_structure.json"
        steps = [
            ("XML 구조 분석", "src/fin_research/ingestion/report_parser.py",
             ["--zip-path", zip_path, "--corp-code", corp.corp_code,
              "--fiscal-year", self.args.fiscal_year, "--rcept-no", report.rcept_no]),
            ("연결재무제표 추출", "src/fin_research/ingestion/table_parser.py",
             ["--zip-path", zip_path, "--structure-path", structure]),
            ("연결 주석 추출", "src/fin_research/ingestion/notes_parser.py",
             ["--zip-path", zip_path, "--structure-path", structure]),
            ("XML 최종 점검", "scripts/processing/check_documents.py",
             ["--zip-path", zip_path, "--structure-path", structure,
              "--tables-path", f"{base}_consolidated_tables.json",
              "--notes-path", f"{base}_consolidated_notes.json",
              "--output-path", f"{base}_document_scope.json"]),
        ]
        for label, script, arguments in steps:
            try:
                run_step(label, script, *arguments)
            except StepError as error:
                # XML 분석은 검색 평가에 필수가 아니므로 경고만 남기고 계속한다.
                log(name, f"경고: {error} (검색 평가는 계속 진행)")
                break

    # 11. 평가셋 ---------------------------------------------------------------
    def prepare_dataset(self, corp, name, target: SearchTarget, plan, pages_path: Path) -> tuple[Path | None, str]:
        # 평가셋은 corp/year/scope 폴더 아래에서 접수번호를 파일명으로 사용한다.
        dataset_path = target.dataset_path(PROJECT_ROOT)
        if dataset_path.is_file():
            items = json.loads(dataset_path.read_text(encoding="utf-8"))
            auto = [item.get("annotation_method") == ANNOTATION_METHOD for item in items]
            kind = "auto" if all(auto) else ("mixed" if any(auto) else "manual")
            if not (self.args.regenerate_eval and kind == "auto"):
                if self.args.regenerate_eval:
                    log(name, "사람이 만든 문항이 있는 평가셋이라 다시 만들지 않습니다.")
                log(name, f"기존 평가셋 사용 ({kind}, {len(items)}문항): {dataset_path.name}")
                return dataset_path, kind

        accounts = fetch_financial_accounts(
            self.client, corp.corp_code, target.fiscal_year, target.statement_scope
        )
        if not accounts:
            log(name, "OpenDART 재무제표 API에 값이 없어 평가셋을 자동으로 만들지 못했습니다.")
            return None, "none"

        pages = json.loads(pages_path.read_text(encoding="utf-8"))
        # prepare_pages.py가 경계 쪽을 자른 뒤의 텍스트로 Gold page를 확인한다.
        page_map = {page["pdf_page"]: page["text"] for page in pages}
        items, skipped = build_auto_eval_set(
            accounts, page_map, plan, f"{target.rcept_no}.pdf", target.fiscal_year
        )
        for reason in skipped:
            log(name, f"  문항 제외 - {reason}")
        if not items:
            log(name, "PDF에서 확인된 계정이 없어 평가셋을 만들지 못했습니다.")
            return None, "none"
        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        dataset_path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(name, f"평가셋 자동 생성: {len(items)}문항 -> {dataset_path.name}")
        return dataset_path, "auto"

    def delete_collection(self, name: str, collection_name: str) -> None:
        import chromadb

        client = chromadb.PersistentClient(path=str(self.args.chroma_dir))
        try:
            client.delete_collection(collection_name)
            log(name, f"기존 Chroma 컬렉션 삭제: {collection_name}")
        except Exception:  # 컬렉션이 없으면 지울 것이 없다.
            pass

    # 전체 ---------------------------------------------------------------------
    def resolve_only(self, query: str, row: dict) -> dict:
        corp, entry = self.resolve_corp(query)
        name = (entry or {}).get("name") or corp.corp_name
        report, reason, dcm_no, dcm_source, report_config = self.resolve_report(corp, entry, use_config=False)
        checks = []
        for field, value in (("rcept_no", report.rcept_no),
                             ("dcm_no", dcm_no)):
            expected = report_config.get("rcept_no") if field == "rcept_no" else (report_config.get("official_pdf") or {}).get("dcm_no")
            if expected:
                checks.append(f"{field} {'일치' if str(expected) == value else f'불일치(설정 {expected})'}")
        log(name, f"corp_code={corp.corp_code} 종목코드={corp.stock_code or '-'} "
                  f"rcept_no={report.rcept_no} ({report.report_name}, {reason}) "
                  f"dcm_no={dcm_no} ({dcm_source})"
                  + (f" | companies.yaml: {', '.join(checks)}" if checks else ""))
        row.update(
            company=name, corp_code=corp.corp_code, stock_code=corp.stock_code,
            rcept_no=report.rcept_no, dcm_no=dcm_no,
            status="resolved", message="; ".join([reason, *checks]),
        )
        return row

    def run(self, query: str, row: dict) -> dict:
        """한 기업을 처리한다. 실패해도 그때까지 확인한 값이 row에 남는다."""

        args = self.args

        corp, entry = self.resolve_corp(query)
        name = (entry or {}).get("name") or corp.corp_name
        row.update(company=name, corp_code=corp.corp_code, stock_code=corp.stock_code)
        log(name, f"1. 기업 확인: corp_code={corp.corp_code}, 종목코드={corp.stock_code or '-'}")

        report, reason, dcm_no, dcm_source, _ = self.resolve_report(corp, entry)
        rcept_no = report.rcept_no
        row.update(rcept_no=rcept_no, dcm_no=dcm_no)
        log(name, f"2. 사업보고서: {rcept_no} {report.report_name} ({reason})")
        log(name, f"3. PDF 문서번호: {dcm_no} ({dcm_source})")

        inspection_path = self.collect_pdf(corp, name, rcept_no, dcm_no)
        log(name, f"4. PDF 페이지 텍스트: {inspection_path.name}")

        if args.with_xml:
            self.run_xml(corp, name, report)

        pdf_pages = json.loads(inspection_path.read_text(encoding="utf-8"))["pages"]
        plan = detect_sections(pdf_pages, args.scope)
        scope = plan.statement_scope
        row.update(
            statement_scope=scope,
            statements_pages=page_range_text(plan.statements_range),
            notes_pages=page_range_text(plan.notes_range),
        )
        log(name, f"5. 페이지 범위({scope}, 단위 {plan.unit}): "
                  + ", ".join(r.as_argument() for r in plan.ranges))
        for note in plan.notes:
            log(name, f"   - {note}")

        target = SearchTarget(corp.corp_code, args.fiscal_year, scope, rcept_no)
        # 연결·별도 결과가 같은 공시에서 섞이지 않도록 scope 폴더로 분리하고,
        # 접수번호는 pages·chunks 파일명의 접두사로 남긴다.
        pages_path = target.pages_path(PROJECT_ROOT)
        chunks_path = target.chunks_path(PROJECT_ROOT)
        validation_path = target.validation_path(PROJECT_ROOT)

        section_arguments = []
        for page_range in plan.ranges:
            section_arguments += ["--section-range", page_range.as_argument()]
        run_step("페이지 표준화", "scripts/processing/prepare_pages.py",
                 "--company", name, "--corp-code", corp.corp_code,
                 "--fiscal-year", args.fiscal_year, "--rcept-no", rcept_no,
                 "--statement-scope", scope, *section_arguments,
                 "--output-path", pages_path)
        run_step("청킹", "scripts/processing/build_chunks.py",
                 "--input-path", pages_path, "--output-path", chunks_path,
                 "--chunk-size", args.chunk_size, "--chunk-overlap", args.chunk_overlap)
        run_step("청크 검증", "scripts/processing/check_chunks.py",
                 "--pages-path", pages_path, "--chunks-path", chunks_path,
                 "--output-path", validation_path,
                 "--chunk-size", args.chunk_size)
        row["chunks"] = len(json.loads(chunks_path.read_text(encoding="utf-8")))

        if args.rebuild:
            self.delete_collection(name, target.collection_name)
        try:
            run_step("Chroma 색인", "scripts/storage/build_chroma.py",
                     "--corp-code", corp.corp_code, "--fiscal-year", args.fiscal_year,
                     "--statement-scope", scope, "--rcept-no", rcept_no,
                     "--persist-directory", args.chroma_dir)
        except StepError as error:
            raise StepError(f"{error}. 청크가 바뀌었다면 --rebuild를 붙여 다시 실행하세요.") from None

        if args.skip_eval:
            row.update(status="indexed", message="--skip-eval")
            return row

        dataset_path, kind = self.prepare_dataset(corp, name, target, plan, pages_path)
        row["dataset"] = kind
        if dataset_path is None:
            row.update(status="indexed", message="평가셋 없음")
            return row

        run_step("Hit@K 평가", "scripts/evaluation/evaluate_retrieval.py",
                 "--corp-code", corp.corp_code, "--fiscal-year", args.fiscal_year,
                 "--statement-scope", scope, "--rcept-no", rcept_no,
                 "--dataset-path", dataset_path, "--chroma-path", args.chroma_dir)

        # 평가 결과도 corp/year/scope 폴더 안에서 접수번호를 파일명으로 사용한다.
        result_path = target.result_path(PROJECT_ROOT)
        report_json = json.loads(result_path.read_text(encoding="utf-8"))
        row["questions"] = report_json["config"]["questions"]
        for method in METHODS:
            summary = report_json["summary"][method]
            row[f"{method}_hit3"] = round(summary["hit_at_3"] * 100, 1)
            row[f"{method}_hit5"] = round(summary["hit_at_5"] * 100, 1)
        row.update(status="evaluated", message=reason)

        manifest = {
            **row,
            "dcm_source": dcm_source,
            "section_plan": plan.as_dict(),
            "dataset_path": str(dataset_path.relative_to(PROJECT_ROOT)),
            "result_path": str(result_path.relative_to(PROJECT_ROOT)),
        }
        pipeline_path = target.pipeline_path(PROJECT_ROOT)
        pipeline_path.parent.mkdir(parents=True, exist_ok=True)
        pipeline_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return row


# ---------------------------------------------------------------------------
# 요약 저장·출력
# ---------------------------------------------------------------------------

def save_summary(rows: list[dict], summary_path: Path) -> None:
    existing: dict[tuple, dict] = {}
    if summary_path.is_file():
        with summary_path.open(encoding="utf-8-sig", newline="") as file:
            for old in csv.DictReader(file):
                # 정정공시는 같은 기업·연도·기준이어도 접수번호가 달라 별도 행으로 보존한다.
                existing[(old["corp_code"], old["fiscal_year"], old["statement_scope"], old.get("rcept_no", ""))] = old
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        if not row.get("corp_code"):
            continue
        row = {**row, "updated_at": now}
        # CSV에서 다시 읽은 값은 모두 문자열이지만, 이번 실행의 fiscal_year는 정수다.
        # 저장 직전에 식별값을 문자열로 통일해야 기존 행과 함께 안전하게 정렬할 수 있다.
        corp_code = str(row["corp_code"])
        fiscal_year = str(row["fiscal_year"])
        scope = str(row.get("statement_scope", ""))
        rcept_no = str(row.get("rcept_no", ""))
        key = (corp_code, fiscal_year, scope, rcept_no)
        if scope:
            # 범위 판정 전에 실패했던 같은 기업·연도 행은 새 결과로 대체한다.
            existing.pop((corp_code, fiscal_year, "", ""), None)
        record = {field: row.get(field, "") for field in SUMMARY_FIELDS}
        record.update(
            corp_code=corp_code,
            fiscal_year=fiscal_year,
            statement_scope=scope,
            rcept_no=rcept_no,
        )
        existing[key] = record
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    # 정렬이 끝나기 전에 파일을 열면 예외 발생 시 기존 CSV가 빈 파일로 남을 수 있다.
    # 모든 행을 먼저 확정한 다음에만 파일을 새로 쓴다.
    ordered_rows = sorted(
        existing.values(),
        key=lambda record: (str(record["company"]), str(record["fiscal_year"])),
    )
    # 엑셀에서 한글이 깨지지 않도록 BOM 포함 UTF-8로 저장
    with summary_path.open("w", encoding="utf-8-sig", newline="") as file:
        # 운영체제에 따라 CRLF로 바뀌어 불필요한 Git 변경으로 보이지 않게 LF로 고정한다.
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(ordered_rows)


def print_summary(rows: list[dict]) -> None:
    print("\n=== 전체 결과 (Hit@3 / Hit@5, %) ===")
    print(f"{'기업':<14}{'기준':<14}{'문항':>4}  " + "  ".join(f"{m:>13}" for m in METHODS) + "  상태")
    for row in rows:
        scores = "  ".join(
            f"{row.get(f'{m}_hit3', '-'):>6}/{row.get(f'{m}_hit5', '-'):<6}" for m in METHODS
        )
        print(f"{row.get('company', ''):<14}{row.get('statement_scope', '-'):<14}"
              f"{row.get('questions', '-'):>4}  {scores}  {row.get('status')}"
              + (f" - {row['message']}" if row.get("status") == "failed" else ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="기업별 수집~청킹~색인~Hit@K 평가 일괄 실행")
    parser.add_argument("--companies", nargs="+", default=[], help="기업명, 6자리 종목코드 또는 8자리 고유번호")
    parser.add_argument("--from-config", action="store_true", help="configs/companies.yaml의 기업 전체 실행")
    parser.add_argument("--fiscal-year", type=int, default=2025)
    parser.add_argument("--scope", choices=["auto", "consolidated", "separate"], default="auto",
                        help="auto: 연결재무제표가 있으면 연결, 없으면 별도")
    parser.add_argument("--report-choice", choices=["latest", "original"], default="latest",
                        help="정정공시가 있을 때 최신 접수본(latest) 또는 최초 접수본(original)")
    parser.add_argument("--resolve-only", action="store_true",
                        help="corp_code·rcept_no·dcm_no만 조회하고 companies.yaml 값과 비교")
    parser.add_argument("--rebuild", action="store_true", help="PDF 텍스트 추출·Chroma 컬렉션을 다시 만든다")
    parser.add_argument("--regenerate-eval", action="store_true", help="자동 생성 평가셋을 다시 만든다 (사람이 만든 평가셋은 유지)")
    parser.add_argument("--with-xml", action="store_true", help="XML 원본 ZIP 수집과 XML 재무제표 분석도 실행")
    parser.add_argument("--skip-eval", action="store_true", help="Chroma 색인까지만 실행")
    parser.add_argument("--chunk-size", type=int, default=600)
    parser.add_argument("--chunk-overlap", type=int, default=100)
    parser.add_argument("--chroma-dir", type=Path, default=DEFAULT_CHROMA_DIR)
    parser.add_argument("--summary-path", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--refresh-corp-codes", action="store_true", help="OpenDART 고유번호 파일을 새로 받는다")
    parser.add_argument("--sleep", type=float, default=1.0, help="기업 사이 대기 시간(초). DART 서버 부하 방지")
    args = parser.parse_args()
    if args.from_config:
        args.companies += [entry["name"] for entry in load_config() if entry.get("name")]
    if not args.companies:
        parser.error("--companies 또는 --from-config 중 하나는 필요합니다.")
    return args


def main() -> int:
    args = parse_args()
    try:
        pipeline = Pipeline(args)
    except DartError as error:
        print(f"시작 실패: {error}", file=sys.stderr)
        return 1

    rows: list[dict] = []
    for index, query in enumerate(dict.fromkeys(args.companies)):
        if index:
            time.sleep(args.sleep)
        print(f"\n{'=' * 70}\n[{index + 1}/{len(set(args.companies))}] {query}\n{'=' * 70}", flush=True)
        row: dict = {"company": query, "fiscal_year": args.fiscal_year}
        try:
            if args.resolve_only:
                pipeline.resolve_only(query, row)
            else:
                pipeline.run(query, row)
        except (DartError, PdfDocumentError, SectionDetectionError, StepError) as error:
            print(f"[{query}] 실패: {error}", file=sys.stderr, flush=True)
            row.update(status="failed", message=str(error))
        except Exception as error:  # 한 기업의 예상 못 한 오류로 전체 실행이 멈추지 않게 한다.
            print(f"[{query}] 실패({type(error).__name__}): {error}", file=sys.stderr, flush=True)
            row.update(status="failed", message=f"{type(error).__name__}: {error}")
        rows.append(row)

    if not args.resolve_only:
        print_summary(rows)
        save_summary(rows, args.summary_path)
        print(f"\n요약 CSV: {args.summary_path}")
    failed = [row for row in rows if row.get("status") == "failed"]
    if failed:
        print(f"\n실패 {len(failed)}건: " + ", ".join(row["company"] for row in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
