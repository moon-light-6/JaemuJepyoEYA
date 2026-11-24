"""OpenDART XML 구조 분석기의 기본 테스트."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.report_parser import analyze_report_zip, normalize_xml_text


SAMSUNG_ZIP = PROJECT_ROOT / "data" / "raw" / "dart" / "00126380" / "2025" / "20260310002820.zip"


def financial_table(title: str) -> str:
    """실제 재무제표 표로 판정할 수 있는 최소 XML 표를 만든다."""
    rows = "".join(f"<TR><TD>계정{i}</TD><TD>{i}</TD></TR>" for i in range(6))
    header = f"<TABLE><TR><TD>{title}</TD><TD>2025년 12월 31일</TD></TR><TR></TR><TR></TR></TABLE>"
    return f"{header}<TABLE>{rows}</TABLE>"


class ReportParserTest(unittest.TestCase):
    def test_normalizes_common_dart_xml_notation_without_touching_source(self):
        normalized, repairs = normalize_xml_text("<DOCUMENT><P>R&D < 시장 ></P></DOCUMENT>")
        self.assertIn("R&amp;D", normalized)
        self.assertIn("&lt; 시장 >", normalized)
        self.assertEqual(repairs["escaped_ampersands"], 1)
        self.assertEqual(repairs["escaped_bare_less_than"], 1)

    def test_finds_real_consolidated_tables_and_notes(self):
        tables = "".join(
            financial_table(title)
            for title in [
                "연결 재무상태표",
                "연결 손익계산서",
                "연결 포괄손익계산서",
                "연결 자본변동표",
                "연결 현금흐름표",
            ]
        )
        xml = f"""<DOCUMENT>
<DOCUMENT-NAME>연결감사보고서</DOCUMENT-NAME><BODY>
<TITLE ATOC=\"Y\" ATOCID=\"3\">(첨부) 연결 재무제표</TITLE>
{tables}
<TITLE ATOC=\"Y\" ATOCID=\"4\">주석</TITLE>
<P>1. 일반사항</P><TABLE><TR><TD>주석 본문</TD></TR></TABLE>
<TITLE ATOC=\"Y\" ATOCID=\"5\">기타</TITLE>
</BODY></DOCUMENT>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            zip_path = Path(temporary_directory) / "report.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("consolidated.xml", xml)
            result = analyze_report_zip(zip_path, "00126380", 2025, "20260310002820")

        self.assertTrue(result["verification"]["all_consolidated_financial_statements_present"])
        self.assertTrue(result["verification"]["consolidated_financial_statement_notes_present"])
        self.assertEqual(len(result["consolidated_sections"]), 6)
        for section in result["consolidated_sections"]:
            self.assertEqual(section["source_xml"], "consolidated.xml")
            self.assertIn("xpath", section["location"])

    def test_toc_keyword_alone_is_not_a_financial_statement(self):
        xml = """<DOCUMENT><DOCUMENT-NAME>연결감사보고서</DOCUMENT-NAME><BODY>
<TABLE><TR><TD>연결 재무상태표</TD></TR></TABLE>
</BODY></DOCUMENT>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            zip_path = Path(temporary_directory) / "report.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("consolidated.xml", xml)
            result = analyze_report_zip(zip_path, "00126380", 2025, "20260310002820")

        self.assertFalse(result["verification"]["all_consolidated_financial_statements_present"])
        self.assertEqual(result["consolidated_sections"], [])

    @unittest.skipUnless(SAMSUNG_ZIP.exists(), "삼성전자 OpenDART 원본 ZIP이 없습니다.")
    def test_samsung_2025_consolidated_statement_header_and_data_pairs(self):
        """실제 삼성전자 XML의 제목 TABLE과 데이터 TABLE 분리 구조를 회귀 검증한다."""
        result = analyze_report_zip(SAMSUNG_ZIP, "00126380", 2025, "20260310002820")
        sections = {
            section["section_key"]: section
            for section in result["consolidated_sections"]
            if section["source_xml"] == "20260310002820_00761.xml"
            and section["section_key"] != "consolidated_financial_statement_notes"
        }

        self.assertTrue(
            result["verification"]["all_consolidated_financial_statements_present"],
            "연결감사보고서에서 재무제표 5종을 찾지 못했습니다.",
        )
        self.assertTrue(
            result["verification"]["consolidated_financial_statement_notes_present"],
            "연결재무제표 주석의 구조와 본문을 찾지 못했습니다.",
        )
        self.assertEqual(len(sections), 5, "연결감사보고서의 재무제표 표 수가 5개가 아닙니다.")
        for section in sections.values():
            self.assertNotEqual(
                section["title_location"],
                section["data_location"],
                f"{section['title']}의 제목 TABLE과 데이터 TABLE이 분리되어야 합니다.",
            )
            self.assertGreaterEqual(
                section["evidence"]["financial_row_count"],
                3,
                f"{section['title']}의 계정과목·금액 행이 부족합니다.",
            )


if __name__ == "__main__":
    unittest.main()
