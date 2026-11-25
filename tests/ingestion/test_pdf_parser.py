"""공식 DART PDF의 실제 페이지별 텍스트 점검 회귀 테스트다."""

from __future__ import annotations

import json
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.pdf_parser import (
    PdfDocumentError,
    download_official_pdf,
    inspect_pdf,
    sha256_file,
)


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
        
        # 저장된 메타데이터와 실제 PDF의 해시가 일치하는지 확인
        self.assertEqual(
            metadata["sha256"],
            sha256_file(PDF_PATH),
        )

        # 문서 식별 정보 확인
        self.assertEqual(metadata["rcept_no"], "20260310002820")
        self.assertEqual(str(metadata["dcm_no"]), "11104488")

        self.assertTrue(result["source"]["official_source_verified"])
        self.assertEqual(result["source"]["source_host"], "dart.fss.or.kr")
        self.assertEqual(result["source"]["sha256"], sha256_file(PDF_PATH))
        self.assertGreater(result["summary"]["page_count"], 0)
        self.assertTrue(result["summary"]["text_extractable"])
        self.assertEqual(result["summary"]["blank_page_numbers"], [])
        self.assertEqual(result["summary"]["replacement_character_page_numbers"], [])
        self.assertFalse(result["format_validation"]["xml_comparison_performed"])
        self.assertEqual(result["pages"][0]["source"], {"format": "pdf", "page_number": 1})

    # PDF 변경 안하고 가짜 PDF 응답으로 오류상황 재현!
    def test_invalid_pdf_removes_temporary_file(self):
        """PDF 유효성 검사에 실패하면 임시 파일을 삭제한다."""

        source_url = (
            "https://dart.fss.or.kr/pdf/download/pdf.do"
            "?rcp_no=20260310002820&dcm_no=11104488"
        )

        # 실제 네트워크 요청 대신 가짜 응답 사용
        response = MagicMock()
        response.url = source_url
        response.content = b"%PDF-1.7\n"
        response.headers = {"Content-Type": "application/pdf"}

        session = MagicMock()
        session.get.return_value = response

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "test.pdf"
            part_path = Path(temp_dir) / "test.pdf.part"

            with (
                patch(
                    "fin_research.ingestion.pdf_parser.requests.Session",
                    return_value=session,
                ),
                patch(
                    "fin_research.ingestion.pdf_parser.fitz.open"
                ) as mock_open,
            ):
                # PDF를 열 수 있지만 페이지가 0개인 상황 재현
                document = mock_open.return_value.__enter__.return_value
                document.is_pdf = True
                document.page_count = 0

                with self.assertRaises(PdfDocumentError):
                    download_official_pdf(source_url, pdf_path)

            # 실패한 PDF와 임시 파일 모두 남지 않아야 함
            self.assertFalse(pdf_path.exists())
            self.assertFalse(part_path.exists())


if __name__ == "__main__":
    unittest.main()
