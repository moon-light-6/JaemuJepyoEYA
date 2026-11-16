"""경계 쪽 자르기(--cut-above, --cut-below) 동작을 확인한다."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts.prepare_pages import (
    PageCut,
    SectionRange,
    clipped_page_text,
    parse_cut_above,
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


if __name__ == "__main__":
    unittest.main()
