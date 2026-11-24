"""경계 쪽 자르기(--cut-above, --cut-below) 동작을 확인한다."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.processing.prepare_pages import (
    PageCut,
    SectionRange,
    clipped_page_text,
    find_heading_y,
    has_body_text,
    parse_cut_above,
    plan_auto_cuts,
    parse_cut_below,
    validate_cuts,
)


def make_pdf(path: Path) -> None:
    """위쪽에 연결 내용, 중간에 제목, 아래쪽에 별도 내용이 있는 한 쪽짜리 PDF를 만든다."""

    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), "UPPER-CONSOLIDATED-TAIL")
    page.insert_text((72, 400), "HEADING-FOUR")
    page.insert_text((72, 700), "LOWER-SEPARATE-BODY")
    document.save(path)
    document.close()


class ParseCutTest(unittest.TestCase):
    def test_parse_keeps_heading_with_spaces_and_dots(self):
        cut = parse_cut_above("141:4. 재무제표")
        self.assertEqual(cut, PageCut(141, "4. 재무제표", "above"))
        self.assertEqual(parse_cut_below("54:2. 연결재무제표").keep, "below")

    def test_parse_rejects_wrong_format(self):
        import argparse

        with self.assertRaises(argparse.ArgumentTypeError):
            parse_cut_above("제목만")


class ValidateCutsTest(unittest.TestCase):
    ranges = [SectionRange(48, 141, "연결")]

    def test_duplicate_page_is_rejected(self):
        cuts = [PageCut(141, "a", "above"), PageCut(141, "b", "below")]
        with self.assertRaises(ValueError):
            validate_cuts(cuts, self.ranges)

    def test_page_outside_range_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_cuts([PageCut(200, "a", "above")], self.ranges)

    def test_valid_cut_is_mapped_by_page(self):
        mapped = validate_cuts([PageCut(141, "a", "above")], self.ranges)
        self.assertEqual(set(mapped), {141})


class ClippedTextTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.pdf_path = Path(self.directory.name) / "sample.pdf"
        make_pdf(self.pdf_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_above_keeps_only_text_before_heading(self):
        text = clipped_page_text(self.pdf_path, PageCut(1, "HEADING-FOUR", "above"))
        self.assertIn("UPPER-CONSOLIDATED-TAIL", text)
        self.assertNotIn("HEADING-FOUR", text)
        self.assertNotIn("LOWER-SEPARATE-BODY", text)

    def test_below_keeps_heading_and_text_after_it(self):
        text = clipped_page_text(self.pdf_path, PageCut(1, "HEADING-FOUR", "below"))
        self.assertIn("HEADING-FOUR", text)
        self.assertIn("LOWER-SEPARATE-BODY", text)
        self.assertNotIn("UPPER-CONSOLIDATED-TAIL", text)

    def test_missing_heading_is_an_error(self):
        with self.assertRaises(ValueError):
            clipped_page_text(self.pdf_path, PageCut(1, "NOT-THERE", "above"))

    def test_page_out_of_pdf_is_an_error(self):
        with self.assertRaises(ValueError):
            clipped_page_text(self.pdf_path, PageCut(5, "HEADING-FOUR", "above"))


def make_pages_pdf(path: Path, pages: list[list[tuple[int, str]]]) -> None:
    """쪽마다 (y좌표, 글자) 목록으로 PDF를 만든다."""

    import pymupdf

    document = pymupdf.open()
    for items in pages:
        page = document.new_page()
        for y, text in items:
            page.insert_text((72, y), text)
    document.save(path)
    document.close()


START = "START-HEADING"
END = "END-HEADING"
RANGES = [SectionRange(2, 2, "연결재무제표"), SectionRange(3, 4, "연결재무제표 주석")]


class AutoCutTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.pdf_path = Path(self.directory.name) / "auto.pdf"

    def tearDown(self):
        self.directory.cleanup()

    def build(self, pages):
        make_pages_pdf(self.pdf_path, pages)
        return plan_auto_cuts(self.pdf_path, RANGES, START, END)

    def test_start_page_with_content_above_heading_is_cut_below(self):
        cuts, extras, _ = self.build(
            [
                [(100, "cover")],
                [(100, "SEPARATE-SUMMARY"), (400, START), (500, "BODY")],
                [(100, "BODY-3")],
                [(100, "BODY-4")],
                [(100, END), (500, "SEPARATE")],
            ]
        )
        self.assertEqual(cuts[2], PageCut(2, START, "below"))
        self.assertEqual(extras, {})

    def test_clean_start_and_clean_end_change_nothing(self):
        cuts, extras, notes = self.build(
            [
                [(100, "cover")],
                [(120, START), (500, "BODY")],
                [(100, "BODY-3")],
                [(100, "BODY-4")],
                [(120, END), (500, "SEPARATE")],
            ]
        )
        self.assertEqual((cuts, extras, notes), ({}, {}, []))

    def test_tail_above_separate_heading_on_next_page_is_added(self):
        cuts, extras, _ = self.build(
            [
                [(100, "cover")],
                [(120, START), (500, "BODY")],
                [(100, "BODY-3")],
                [(100, "BODY-4")],
                [(100, "NOTE-TAIL"), (400, END), (500, "SEPARATE")],
            ]
        )
        self.assertEqual(cuts, {})
        self.assertEqual(extras, {5: PageCut(5, END, "above")})

    def test_separate_heading_inside_last_page_is_cut_above(self):
        cuts, extras, _ = self.build(
            [
                [(100, "cover")],
                [(120, START), (500, "BODY")],
                [(100, "BODY-3")],
                [(100, "NOTE-TAIL"), (400, END), (500, "SEPARATE")],
                [(100, "SEPARATE-ONLY")],
            ]
        )
        self.assertEqual(cuts, {4: PageCut(4, END, "above")})
        self.assertEqual(extras, {})

    def test_heading_inside_a_sentence_is_ignored(self):
        cuts, extras, _ = self.build(
            [
                [(100, "cover")],
                [(120, START), (500, "BODY")],
                [(100, "BODY-3")],
                [(100, "BODY-4")],
                [(100, "NOTE-TAIL"), (400, "see END-HEADING for details"), (500, "MORE")],
            ]
        )
        self.assertEqual((cuts, extras), ({}, {}))

    def test_find_heading_requires_exactly_one_standalone_line(self):
        make_pages_pdf(
            self.pdf_path,
            [[(100, END), (300, END)]],
        )
        import pymupdf

        with pymupdf.open(self.pdf_path) as document:
            self.assertIsNone(find_heading_y(document[0], END))

    def test_has_body_text_ignores_footer_lines(self):
        self.assertFalse(has_body_text("전자공시시스템 dart.fss.or.kr\n"))
        self.assertFalse(has_body_text(" | \nPage 12 |\n"))
        self.assertTrue(has_body_text("본문\n전자공시시스템 dart.fss.or.kr"))


if __name__ == "__main__":
    unittest.main()
