"""연결재무제표 표 추출기의 기본 및 실제 원문 회귀 테스트."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.table_parser import (
    TableParseError,
    extract_consolidated_tables,
    extract_table_rows,
    section_records_for_extraction,
)


SAMSUNG_ZIP = PROJECT_ROOT / "data" / "raw" / "dart" / "00126380" / "2025" / "20260310002820.zip"
SAMSUNG_STRUCTURE = (
    PROJECT_ROOT / "data" / "processed" / "dart" / "00126380" / "2025" / "20260310002820_structure.json"
)


class TableParserTest(unittest.TestCase):
    def test_preserves_cell_text_and_merged_cell_layout(self):
        table = ElementTree.fromstring(
            "<TABLE><TR><TH ROWSPAN='2' COLSPAN='2'>과 목</TH><TH COLSPAN='2'>기간</TH></TR>"
            "<TR><TD>2025</TD><TD>(1,234)</TD></TR></TABLE>"
        )
        extracted = extract_table_rows(table)

        self.assertEqual(extracted["row_count"], 2)
        self.assertEqual(extracted["logical_column_count"], 4)
        self.assertEqual(extracted["rows"][0]["cells"][0]["text"], "과 목")
        self.assertEqual(extracted["rows"][0]["cells"][0]["rowspan"], 2)
        self.assertEqual(extracted["rows"][0]["cells"][0]["colspan"], 2)
        self.assertEqual(extracted["rows"][0]["cells"][1]["colspan"], 2)
        self.assertEqual(extracted["rows"][1]["cells"][0]["logical_column_start"], 2)
        self.assertEqual(extracted["rows"][1]["cells"][1]["text"], "(1,234)")
        self.assertTrue(extracted["layout_validation"]["no_overlapping_logical_columns"])

    def test_duplicate_section_selection_reports_source_locations(self):
        section = {
            "section_key": "consolidated_income_statement",
            "source_xml": "consolidated.xml",
            "title_location": {"xpath": "/DOCUMENT[1]/TABLE[1]"},
            "data_location": {"xpath": "/DOCUMENT[1]/TABLE[2]"},
        }
        structure = {
            "documents": [{"filename": "consolidated.xml", "role": "consolidated_audit_report"}],
            "consolidated_sections": [section, {**section, "data_location": {"xpath": "/DOCUMENT[1]/TABLE[3]"}}],
        }
        with self.assertRaisesRegex(TableParseError, "중복 선택"):
            section_records_for_extraction(structure)

    @unittest.skipUnless(
        SAMSUNG_ZIP.exists() and SAMSUNG_STRUCTURE.exists(),
        "삼성전자 원본 ZIP 또는 구조 분석 JSON이 없습니다.",
    )
    def test_samsung_2025_tables_match_original_xml_cells(self):
        """실제 삼성전자 연결감사보고서의 표 5개를 원문 셀 단위로 대조한다."""
        result = extract_consolidated_tables(SAMSUNG_ZIP, SAMSUNG_STRUCTURE)

        self.assertEqual(len(result["tables"]), 5)
        self.assertEqual(len({table["section_key"] for table in result["tables"]}), 5)
        for table in result["tables"]:
            self.assertEqual(table["consolidation_basis"], "consolidated")
            self.assertEqual(table["source"]["document_role"], "consolidated_audit_report")
            self.assertTrue(table["validation"]["all_cell_texts_match_source"])
            self.assertTrue(table["validation"]["rowspan_colspan_match_source"])
            self.assertTrue(table["table"]["layout_validation"]["no_overlapping_logical_columns"])
            self.assertTrue(table["accounting_structure_validation"]["all_title_periods_mapped"])
            self.assertTrue(table["accounting_structure_validation"]["all_mapped_periods_have_amount_cells"])
            self.assertTrue(any("2025년" in period for period in table["periods"]))
            self.assertTrue(any("2024년" in period for period in table["periods"]))
            self.assertEqual(table["unit"], "(단위 : 백만원)")

        income_statement = next(
            table
            for table in result["tables"]
            if table["section_key"] == "consolidated_income_statement"
        )
        income_texts = [
            cell["text"]
            for row in income_statement["table"]["rows"]
            for cell in row["cells"]
        ]
        self.assertIn("333,605,938", income_texts)
        self.assertEqual(income_statement["accounting_structure_validation"]["mapping_mode"], "header_period_groups")
        self.assertEqual(
            [mapping["logical_columns"] for mapping in income_statement["period_column_mapping"]],
            [[2, 3], [4, 5]],
        )

        cash_flows = next(
            table
            for table in result["tables"]
            if table["section_key"] == "consolidated_statement_of_cash_flows"
        )
        cash_flow_texts = [
            cell["text"]
            for row in cash_flows["table"]["rows"]
            for cell in row["cells"]
        ]
        self.assertIn("(9,613,906)", cash_flow_texts)
        self.assertIn("-", cash_flow_texts)

        equity = next(
            table
            for table in result["tables"]
            if table["section_key"] == "consolidated_statement_of_changes_in_equity"
        )
        self.assertEqual(equity["accounting_structure_validation"]["mapping_mode"], "row_date_labels")
        self.assertTrue(any("2025.12.31" in label for mapping in equity["period_column_mapping"] for label in mapping["data_row_labels"]))


if __name__ == "__main__":
    unittest.main()
