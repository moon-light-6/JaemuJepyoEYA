"""DART 감사보고서 XML을 검색용 문서 JSON으로 만든다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.xml_documents import XmlDocumentError, build_xml_documents


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="감사보고서 XML을 검색용 문서 JSON으로 정리합니다.")
    parser.add_argument("--company", required=True, help="기업명")
    parser.add_argument("--corp-code", required=True, help="OpenDART 기업 고유번호")
    parser.add_argument("--fiscal-year", required=True, type=int, help="사업연도")
    parser.add_argument("--rcept-no", required=True, help="DART 접수번호")
    parser.add_argument("--statement-scope", required=True, choices=["consolidated", "separate"])
    parser.add_argument("--zip-path", type=Path, help="원본 DART ZIP 경로")
    parser.add_argument("--output-path", type=Path, help="검색용 XML 문서 JSON 경로")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    zip_path = args.zip_path or (
        PROJECT_ROOT / "data" / "raw" / "dart" / args.corp_code
        / str(args.fiscal_year) / f"{args.rcept_no}.zip"
    )
    # XML과 PDF 처리 결과가 섞이지 않도록 XML 전용 폴더에 저장한다.
    output_path = args.output_path or (
        PROJECT_ROOT / "data" / "processed" / "xml" / "dart" / args.corp_code
        / str(args.fiscal_year) / args.statement_scope
        / f"{args.rcept_no}_xml_documents.json"
    )
    documents, metadata = build_xml_documents(
        zip_path, args.corp_code, args.fiscal_year,
        args.statement_scope, args.company,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(documents, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"선택 XML: {metadata['source_xml']}")
    print(f"재무제표 문서: {metadata['financial_statement_documents']}개")
    print(f"주석 문서: {metadata['note_documents']}개")
    print(f"출력 파일: {output_path}")


if __name__ == "__main__":
    try:
        main()
    except XmlDocumentError as error:
        raise SystemExit(f"XML 문서 준비 실패: {error}") from error
