"""설정된 DART 공식 PDF 원본을 수집하고 페이지별 텍스트를 점검한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from collect import load_target
from fin_research.ingestion.dart_client import DartError
from fin_research.ingestion.pdf_parser import (
    PdfDocumentError,
    dart_pdf_download_page_url,
    dart_pdf_url,
    download_official_pdf,
    inspect_pdf,
    save_pdf_inspection,
    sha256_file,
)


def load_reusable_metadata(metadata_path: Path, source_url: str, pdf_path: Path) -> dict | None:
    """동일 공식 URL·해시가 기록된 PDF만 재사용해 중복 다운로드를 막는다."""
    if not (metadata_path.is_file() and pdf_path.is_file()):
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if metadata.get("source_url") != source_url or metadata.get("sha256") != sha256_file(pdf_path):
        return None
    return metadata


def collect_pdf(company_code: str, fiscal_year: int) -> tuple[Path, Path, Path]:
    """기업·연도 설정의 DART 문서번호로 PDF를 수집하고 검증 JSON을 저장한다."""
    company, report = load_target(company_code, fiscal_year)
    pdf_config = report.get("official_pdf", {})
    if pdf_config.get("provider") != "dart" or not pdf_config.get("dcm_no"):
        raise PdfDocumentError("설정에 DART 공식 PDF의 provider와 dcm_no가 필요합니다.")

    rcept_no = report["rcept_no"]
    dcm_no = str(pdf_config["dcm_no"])
    source_url = dart_pdf_url(rcept_no, dcm_no)
    raw_directory = PROJECT_ROOT / "data" / "raw" / "dart" / company_code / str(fiscal_year)
    processed_directory = PROJECT_ROOT / "data" / "processed" / "dart" / company_code / str(fiscal_year)
    pdf_path = raw_directory / f"{rcept_no}.pdf"
    metadata_path = raw_directory / f"{rcept_no}_pdf.json"
    inspection_path = processed_directory / f"{rcept_no}_pdf_pages.json"

    metadata = load_reusable_metadata(metadata_path, source_url, pdf_path)
    if metadata is None:
        if pdf_path.exists() or metadata_path.exists():
            raise PdfDocumentError(
                "기존 PDF 또는 메타데이터의 URL·해시가 일치하지 않습니다. 원본을 덮어쓰지 않았습니다."
            )
        metadata = download_official_pdf(
            source_url,
            pdf_path,
            referer_url=dart_pdf_download_page_url(rcept_no, dcm_no),
        )
        metadata.update(
            {
                "company_name": company.get("name"),
                "company_code": company_code,
                "fiscal_year": fiscal_year,
                "rcept_no": rcept_no,
                "dcm_no": dcm_no,
                "report_type": report.get("report_type"),
            }
        )
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    result = inspect_pdf(pdf_path, metadata)
    save_pdf_inspection(result, inspection_path)
    return pdf_path, metadata_path, inspection_path


def main() -> int:
    """명령행에서 설정된 공식 PDF 수집과 PDF 단독 점검을 실행한다."""
    parser = argparse.ArgumentParser(description="DART 공식 PDF 수집 및 페이지별 텍스트 점검")
    parser.add_argument("--corp-code", required=True, help="OpenDART 고유번호 8자리")
    parser.add_argument("--fiscal-year", required=True, type=int, help="사업연도")
    args = parser.parse_args()
    try:
        pdf_path, metadata_path, inspection_path = collect_pdf(args.corp_code, args.fiscal_year)
    except (DartError, PdfDocumentError) as error:
        print(f"PDF 수집 실패: {error}", file=sys.stderr)
        return 1
    print(f"공식 PDF: {pdf_path}")
    print(f"출처 메타데이터: {metadata_path}")
    print(f"페이지 점검 결과: {inspection_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
