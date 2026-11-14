"""설정 파일을 기준으로 OpenDART 원본 사업보고서를 수집한다."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# src 레이아웃을 패키지 설치 전에도 CLI로 바로 실행할 수 있게 한다.
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.dart_client import AmbiguousReportError, DartClient, DartError


def load_target(company_code: str, fiscal_year: int) -> tuple[dict, dict]:
    """기업코드와 사업연도에 해당하는 설정을 읽어 반환한다."""
    config_path = PROJECT_ROOT / "configs" / "companies.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise DartError(f"기업 설정 파일을 읽지 못했습니다: {error}") from error

    for company in config.get("companies", []):
        if company.get("corp_code") == company_code:
            report = company.get("reports", {}).get(str(fiscal_year))
            if report is None:
                break
            return company, report
    raise DartError(f"기업코드 {company_code}, 사업연도 {fiscal_year} 설정을 찾지 못했습니다.")


def collect(company_code: str, fiscal_year: int) -> tuple[Path, Path]:
    """후보 확인, 원본 ZIP 다운로드, 출처 메타데이터 저장을 순서대로 수행한다."""
    company, report = load_target(company_code, fiscal_year)
    client = DartClient.from_environment()
    candidates = client.search_business_reports(
        corp_code=company_code,
        fiscal_year=fiscal_year,
        disclosure_start=report["disclosure_start"],
        disclosure_end=report["disclosure_end"],
    )
    selected = client.resolve_report(candidates, report.get("rcept_no"))
    output_dir = PROJECT_ROOT / "data" / "raw" / "dart" / company_code / str(fiscal_year)
    zip_path = output_dir / f"{selected.rcept_no}.zip"
    client.download_original_zip(selected.rcept_no, zip_path)
    metadata_path = client.save_metadata(selected, zip_path)
    return zip_path, metadata_path


def main() -> int:
    """명령줄 인자를 받아 수집 결과 또는 사람이 확인할 후보 오류를 출력한다."""
    parser = argparse.ArgumentParser(description="OpenDART 사업보고서 원본 ZIP 수집")
    parser.add_argument("--corp-code", default="00126380", help="OpenDART 고유번호 8자리")
    parser.add_argument("--fiscal-year", default=2025, type=int, help="사업연도")
    args = parser.parse_args()

    try:
        zip_path, metadata_path = collect(args.corp_code, args.fiscal_year)
    except AmbiguousReportError as error:
        print(f"확정 필요: {error}", file=sys.stderr)
        return 2
    except DartError as error:
        print(f"수집 실패: {error}", file=sys.stderr)
        return 1
    print(f"원본 ZIP: {zip_path}")
    print(f"출처 메타데이터: {metadata_path}")
    print("연결재무제표·주석 포함 여부는 이 단계에서 검증하지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
