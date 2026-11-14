# [기존 추출 결과 점검]

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.document_check import (
    DocumentCheckError,
    inspect_xml_scope,
    save_document_check,
)


def main() -> int:
    """원본 ZIP과 기존 XML 결과 JSON을 대조해 인수인계용 점검 파일을 저장한다."""
    parser = argparse.ArgumentParser(description="Day 1 XML 문서 범위 최종 점검")
    parser.add_argument("--zip-path", required=True, type=Path)
    parser.add_argument("--structure-path", required=True, type=Path)
    parser.add_argument("--tables-path", required=True, type=Path)
    parser.add_argument("--notes-path", required=True, type=Path)
    parser.add_argument("--output-path", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = inspect_xml_scope(
            args.zip_path, args.structure_path, args.tables_path, args.notes_path
        )
        save_document_check(result, args.output_path)
    except DocumentCheckError as error:
        parser.error(str(error))
    print(f"XML 문서 점검 결과: {args.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
