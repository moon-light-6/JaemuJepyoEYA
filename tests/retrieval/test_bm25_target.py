"""기업별 BM25 검색 대상 연결 테스트."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.bm25_retriever import search_bm25_for_target


class TestBM25Target(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.target = SearchTarget(
            corp_code="00126380",
            fiscal_year=2025,
            statement_scope="consolidated",
            rcept_no="20260310002820",
        )

        self.chunks = [
            {
                "chunk_id": "test_001",
                "text": "매출액 영업이익 재무제표",
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
                "source_file": "20260310002820.pdf",
                "pdf_page": 85,
                "section": "연결재무제표",
            },
            {
                "chunk_id": "test_002",
                "text": "자산총계 부채총계 자본총계",
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
                "source_file": "20260310002820.pdf",
                "pdf_page": 83,
                "section": "연결재무제표",
            },
        ]

        self.path = self.target.chunks_path(self.root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.save_chunks()

    def tearDown(self):
        self.temp_dir.cleanup()

    def save_chunks(self):
        self.path.write_text(
            json.dumps(self.chunks, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_target_search(self):
        results = search_bm25_for_target(
            target=self.target,
            project_root=self.root,
            question="매출액",
            k=2,
        )

        self.assertEqual(len(results), 2)

        result_ids = {result["chunk_id"] for result in results}

        self.assertEqual(
            result_ids,
            {"test_001", "test_002"},
        )

        for result in results:
            self.assertEqual(
                result["metadata"]["statement_scope"],
                "consolidated",
            )
            self.assertEqual(
                result["metadata"]["fiscal_year"],
                2025,
            )
            self.assertEqual(
                result["source_file"],
                "20260310002820.pdf",
            )

    def test_invalid_scope(self):
        self.chunks[0]["statement_scope"] = "separate"
        self.save_chunks()

        with self.assertRaises(ValueError):
            search_bm25_for_target(
                self.target, self.root, "매출액"
            )

    def test_invalid_year(self):
        self.chunks[0]["fiscal_year"] = 2024
        self.save_chunks()

        with self.assertRaises(ValueError):
            search_bm25_for_target(
                self.target, self.root, "매출액"
            )

    def test_invalid_source_file(self):
        self.chunks[0]["source_file"] = "another.pdf"
        self.save_chunks()

        with self.assertRaises(ValueError):
            search_bm25_for_target(
                self.target, self.root, "매출액"
            )

    def test_missing_chunks(self):
        self.path.unlink()

        with self.assertRaises(FileNotFoundError):
            search_bm25_for_target(
                self.target, self.root, "매출액"
            )


if __name__ == "__main__":
    unittest.main()
