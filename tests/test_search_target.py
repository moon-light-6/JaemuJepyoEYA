"""검색 대상 식별자와 경로 생성 단위 테스트."""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.search_target import SearchTarget


class SearchTargetTest(unittest.TestCase):

    def setUp(self):
        self.target = SearchTarget(
            corp_code="00126380",
            fiscal_year=2025,
            statement_scope="consolidated",
            rcept_no="20260310002820",
        )

    def test_chunks_path(self):
        result = self.target.chunks_path(PROJECT_ROOT)

        expected = (
            PROJECT_ROOT / "data" / "processed" / "dart"
            / "00126380" / "2025"
            / "20260310002820_chunks.json"
        )

        self.assertEqual(result, expected)

    def test_collection_name(self):
        self.assertEqual(
            self.target.collection_name(),
            "fin_00126380_2025_consolidated_20260310002820_kure_v1",
        )

    def test_source_file(self):
        self.assertEqual(
            self.target.source_file(),
            "20260310002820.pdf",
        )

    def test_invalid_corp_code(self):
        with self.assertRaises(ValueError):
            SearchTarget("123", 2025, "consolidated", "20260310002820")

    def test_invalid_fiscal_year(self):
        with self.assertRaises(ValueError):
            SearchTarget("00126380", 1800, "consolidated", "20260310002820")

    def test_invalid_statement_scope(self):
        with self.assertRaises(ValueError):
            SearchTarget("00126380", 2025, "unknown", "20260310002820")

    def test_invalid_rcept_no(self):
        with self.assertRaises(ValueError):
            SearchTarget("00126380", 2025, "consolidated", "123")

    def test_different_companies_have_different_collections(self):
        other = SearchTarget(
            corp_code="12345678",
            fiscal_year=2025,
            statement_scope="consolidated",
            rcept_no="20260310002820",
        )

        self.assertNotEqual(
            self.target.collection_name(),
            other.collection_name(),
        )

    def test_different_reports_have_different_collections(self):
        other = SearchTarget(
            corp_code="00126380",
            fiscal_year=2025,
            statement_scope="consolidated",
            rcept_no="20260401000001",
        )

        self.assertNotEqual(
            self.target.collection_name(),
            other.collection_name(),
        )


if __name__ == "__main__":
    unittest.main()
