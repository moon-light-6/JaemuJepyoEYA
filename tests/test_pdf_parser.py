"""공식 DART PDF의 실제 페이지별 텍스트 점검 회귀 테스트다."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.pdf_parser import inspect_pdf, sha256_file


PDF_PATH = PROJECT_ROOT / "data" / "raw" / "dart" / "00126380" / "2025" / "20260310002820.pdf"
METADATA_PATH = PDF_PATH.with_name("20260310002820_pdf.json")


class PdfParserTest(unittest.TestCase):
    @unittest.skipUnless(
        PDF_PATH.exists() and METADATA_PATH.exists(),
        "삼성전자 공식 DART PDF 또는 출처 메타데이터가 없습니다.",
    )
    def test_samsung_2025_official_pdf_pages_are_extractable(self):
        """공식 PDF의 해시·페이지 출처·빈 페이지·대체문자를 실제 파일로 점검한다."""
        metadata = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
        result = inspect_pdf(PDF_PATH, metadata)

        self.assertTrue(result["source"]["official_source_verified"])
        self.assertEqual(result["source"]["source_host"], "dart.fss.or.kr")
        self.assertEqual(result["source"]["sha256"], sha256_file(PDF_PATH))
        self.assertGreater(result["summary"]["page_count"], 0)
        self.assertTrue(result["summary"]["text_extractable"])
        self.assertEqual(result["summary"]["blank_page_numbers"], [])
        self.assertEqual(result["summary"]["replacement_character_page_numbers"], [])
        self.assertFalse(result["format_validation"]["xml_comparison_performed"])
        self.assertEqual(result["pages"][0]["source"], {"format": "pdf", "page_number": 1})


if __name__ == "__main__":
    unittest.main()
