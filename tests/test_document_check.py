"""Day 1 XML 범위 점검의 실제 삼성전자 원문 회귀 테스트다."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.document_check import (
    DocumentCheckError,
    inspect_xml_scope,
)


BASE = PROJECT_ROOT / "data"
RAW = BASE / "raw" / "dart" / "00126380" / "2025"
PROCESSED = BASE / "processed" / "dart" / "00126380" / "2025"
ZIP_PATH = RAW / "20260310002820.zip"
STRUCTURE_PATH = PROCESSED / "20260310002820_structure.json"
TABLES_PATH = PROCESSED / "20260310002820_consolidated_tables.json"
NOTES_PATH = PROCESSED / "20260310002820_consolidated_notes.json"


class DocumentCheckTest(unittest.TestCase):
    @unittest.skipUnless(
        all(path.exists() for path in (ZIP_PATH, STRUCTURE_PATH, TABLES_PATH, NOTES_PATH)),
        "삼성전자 원문 또는 기존 XML 추출 결과가 없습니다.",
    )
    def test_samsung_2025_xml_scope_is_complete(self):
        """연결재무제표·비교 열·주석과 사업보고서 본문 위치를 원문으로 재점검한다."""
        result = inspect_xml_scope(ZIP_PATH, STRUCTURE_PATH, TABLES_PATH, NOTES_PATH)

        statements = result["consolidated_financial_statements"]
        self.assertEqual(statements["extracted_count"], 5)
        self.assertEqual(statements["missing_section_keys"], [])
        self.assertEqual(statements["duplicate_section_keys"], [])
        for statement in statements["statements"]:
            self.assertTrue(statement["has_current_year"])
            self.assertTrue(statement["has_previous_year"])
            self.assertTrue(statement["period_mapping_verified"])
            self.assertTrue(statement["raw_cell_preservation_verified"])

        notes = result["consolidated_notes"]
        self.assertEqual(notes["extracted_note_count"], 34)
        self.assertEqual(notes["missing_note_numbers"], [])
        self.assertEqual(notes["review_required"], [])
        self.assertEqual(notes["source_table_count"], 320)
        self.assertEqual(notes["extracted_table_count"], 320)

        topics = result["business_report_topics"]
        self.assertTrue(topics["business_overview"]["has_substantive_body"])
        self.assertTrue(topics["audit_opinion"]["has_substantive_body"])
        self.assertIn("SECTION-1[3]", topics["business_overview"]["source"]["xpath"])
        self.assertIn("SECTION-1[6]", topics["audit_opinion"]["source"]["xpath"])

    # 사업연도 틀리면 원본 XML 처리 전 오류 발생시키는지 확인용
    def test_rejects_mismatched_report_sources(self):
        """서로 다른 공시의 JSON이 섞이면 검증을 중단한다."""

        source = {
            "company_code": "00126380",
            "fiscal_year": 2025,
            "rcept_no": "20260310002820",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)

            structure_path = folder / "structure.json"
            tables_path = folder / "tables.json"
            notes_path = folder / "notes.json"

            structure_path.write_text(
                json.dumps({"source": source}),
                encoding="utf-8",
            )

            # 잘못된 연도가 포함된 재무제표
            wrong_source = {
                **source,
                "fiscal_year": 2024,
            }

            tables_path.write_text(
                json.dumps({"source": wrong_source}),
                encoding="utf-8",
            )

            notes_path.write_text(
                json.dumps({"source": source}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DocumentCheckError, "fiscal_year"
            ):
                inspect_xml_scope(
                    folder / "unused.zip",
                    structure_path,
                    tables_path,
                    notes_path,
                )

if __name__ == "__main__":
    unittest.main()
