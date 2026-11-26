"""청크 품질 점검기의 핵심 검증 규칙을 확인한다."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.processing.check_chunks import validate_chunks


def page(page_id: str = "corp_2025_consolidated_0001") -> dict:
    return {
        "page_id": page_id,
        "text": "연결재무상태표 자산총계와 부채총계를 확인합니다.",
        "company": "테스트회사",
        "fiscal_year": 2025,
        "statement_scope": "consolidated",
        "document_type": "financial_statement",
        "source_file": "test.pdf",
        "pdf_page": 1,
        "section": "연결재무제표",
    }


def chunk(chunk_id: str = "corp_2025_consolidated_0001_01") -> dict:
    source = page()
    return {"chunk_id": chunk_id, "text": "연결재무상태표 자산총계", **source}


class ChunkCheckTest(unittest.TestCase):
    def test_valid_chunks_pass(self):
        result = validate_chunks([page()], [chunk()], chunk_size=600)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["summary"]["pages_represented_by_chunks"], 1)

    def test_detects_metadata_and_source_errors(self):
        invalid = chunk()
        invalid["pdf_page"] = 99
        invalid["text"] = "원본에 없는 문장"
        result = validate_chunks([page()], [invalid], chunk_size=600)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["violations"]["metadata_mismatches"]), 1)
        self.assertEqual(result["violations"]["text_not_in_source_chunk_ids"], [invalid["chunk_id"]])


if __name__ == "__main__":
    unittest.main()
