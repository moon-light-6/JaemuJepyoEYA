"""연결재무제표 주석 추출기의 실제 원문 회귀 테스트."""

from __future__ import annotations

import sys
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.notes_parser import (
    extract_consolidated_notes,
    extract_notes_from_container,
)
from fin_research.ingestion.report_parser import (
    build_locations,
    compact_text,
    normalize_xml_text,
)
from fin_research.ingestion.table_parser import find_by_xpath, raw_cell_text


SAMSUNG_ZIP = PROJECT_ROOT / "data" / "raw" / "dart" / "00126380" / "2025" / "20260310002820.zip"
SAMSUNG_STRUCTURE = (
    PROJECT_ROOT / "data" / "processed" / "dart" / "00126380" / "2025" / "20260310002820_structure.json"
)


class NotesParserTest(unittest.TestCase):
    @unittest.skipUnless(
        SAMSUNG_ZIP.exists() and SAMSUNG_STRUCTURE.exists(),
        "삼성전자 원본 ZIP 또는 구조 분석 JSON이 없습니다.",
    )
    def test_samsung_2025_consolidated_notes_match_original_xml(self):
        """주석 1~3번과 첫 표를 실제 연결감사보고서 원문에 대조한다."""
        result = extract_consolidated_notes(SAMSUNG_ZIP, SAMSUNG_STRUCTURE)

        self.assertEqual(result["summary"]["note_count"], 34)
        self.assertEqual(result["summary"]["missing_note_numbers"], [])
        self.assertEqual(result["summary"]["preamble_item_count"], 3)
        self.assertEqual(result["summary"]["preamble_table_count"], 1)
        self.assertEqual(result["summary"]["table_count"], 319)
        self.assertEqual(result["summary"]["total_table_count_including_preamble"], 320)
        self.assertTrue(result["boundary_validation"]["container_within_structure_analysis_range"])
        self.assertTrue(result["coverage_validation"]["all_source_tables_extracted"])
        self.assertTrue(result["coverage_validation"]["all_direct_elements_represented"])
        self.assertTrue(result["coverage_validation"]["source_order_preserved"])
        self.assertEqual(result["coverage_validation"]["missing_table_xpaths"], [])
        self.assertEqual(result["duplicate_comparison"]["same_note_numbers"], list(range(1, 35)))
        self.assertFalse(result["duplicate_comparison"]["content_merge_performed"])

        with zipfile.ZipFile(SAMSUNG_ZIP) as archive:
            root = ElementTree.fromstring(
                normalize_xml_text(archive.read("20260310002820_00761.xml").decode("utf-8"))[0]
            )

        for note in result["notes"][:3]:
            heading = find_by_xpath(root, note["source"]["xpath"])
            self.assertEqual(compact_text(heading), note["title"])
            self.assertEqual(note["source"]["xml_filename"], "20260310002820_00761.xml")
            self.assertEqual(note["source"]["rcept_no"], "20260310002820")

        first_table = next(item for item in result["notes"][0]["items"] if item["kind"] == "table")
        source_table = find_by_xpath(root, first_table["source"]["xpath"])
        self.assertTrue(first_table["raw_cell_preservation"]["all_cell_texts_match_source"])
        self.assertTrue(first_table["raw_cell_preservation"]["rowspan_colspan_match_source"])
        first_source_cell = next(
            cell
            for row in source_table.iter("TR")
            for cell in row
            if cell.tag in {"TD", "TH", "TU"}
        )
        self.assertEqual(
            raw_cell_text(first_source_cell),
            first_table["table"]["rows"][0]["cells"][0]["text"],
        )

        # 제목과 첫 본문이 같은 P에 붙은 주석은 사업보고서의 동일 번호 제목으로만 분리한다.
        inline_body_numbers = [
            note["number"]
            for note in result["notes"]
            if note["title_boundary"]["status"] == "separated_with_business_report_reference"
        ]
        self.assertEqual(inline_body_numbers, [13, 17, 28, 30, 32, 33, 34])

        note_34 = result["notes"][-1]
        self.assertEqual(note_34["number"], 34)
        self.assertEqual(note_34["title"], "34. 보고기간후사건")
        self.assertEqual(
            note_34["source"]["xpath"],
            "/DOCUMENT[1]/BODY[1]/SECTION-1[1]/SECTION-2[1]/P[375]",
        )
        self.assertEqual(note_34["title_boundary"]["source_has_child_elements"], False)
        self.assertEqual(
            note_34["title"] + note_34["items"][0]["text"],
            note_34["heading_raw_text"],
        )
        self.assertEqual(note_34["items"][0]["kind"], "paragraph")
        self.assertEqual(note_34["items"][0]["source"]["xpath"], note_34["source"]["xpath"])

    # 테스트 함수 추가
    def test_ignores_numbered_paragraph_before_notes_heading(self):
        """주석 제목 이전의 번호 문단은 주석으로 추출하지 않는다."""

        xml = """
        <SECTION-2>
            <P>1. 주석 이전 안내문</P>
            <TITLE>주석</TITLE>
            <P>1. 실제 주석</P>
            <P>주석 본문입니다.</P>
        </SECTION-2>
        """

        root = ElementTree.fromstring(xml)
        _, locations = build_locations(root)

        heading = root.find("TITLE")

        preamble, notes = extract_notes_from_container(
            container=root,
            heading=heading,
            filename="test.xml",
            rcept_no="20260310002820",
            locations=locations,
            business_titles=[],
        )

        # 주석 제목 이전의 '1. 안내문'은 제외
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["number"], 1)

        # 주석 제목 이후의 실제 본문만 추출
        self.assertEqual(len(notes[0]["items"]), 1)
        self.assertEqual(
            notes[0]["items"][0]["text"],
            "주석 본문입니다.",
        )

        self.assertEqual(preamble, [])
        
if __name__ == "__main__":
    unittest.main()
