"""SearchTarget의 검색 대상 식별 및 경로 생성 테스트."""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.search_target import SearchTarget


class SearchTargetTest(unittest.TestCase):

    def setUp(self):
        """각 테스트에서 사용할 삼성전자 검색 대상을 생성한다."""
        self.target = SearchTarget(
            corp_code="00126380",
            fiscal_year=2025,
            statement_scope="consolidated",
            rcept_no="20260310002820",
        )

    # 1. Chroma 컬렉션 이름 생성
    def test_collection_name(self):
        expected = (
            "fin_00126380_2025_consolidated_"
            "20260310002820_kure_v1"
        )

        self.assertEqual(self.target.collection_name, expected)

    # 2. 검색 산출물은 corp/year/scope 폴더 아래에 접수번호 파일명으로 생성
    def test_chunks_path(self):
        expected = (
            PROJECT_ROOT
            / "data"
            / "processed"
            / "dart"
            / "00126380"
            / "2025"
            / "consolidated"
            / "20260310002820_chunks.json"
        )

        self.assertEqual(
            self.target.chunks_path(PROJECT_ROOT),
            expected,
        )

    # 3. 연결/별도 컬렉션 이름 구분
    def test_different_statement_scope(self):
        separate_target = SearchTarget(
            corp_code="00126380",
            fiscal_year=2025,
            statement_scope="separate",
            rcept_no="20260310002820",
        )

        self.assertNotEqual(
            self.target.collection_name,
            separate_target.collection_name,
        )

        # 동일 공시라도 연결과 별도는 각각 다른 산출물 폴더를 사용한다.
        self.assertNotEqual(
            self.target.chunks_path(PROJECT_ROOT),
            separate_target.chunks_path(PROJECT_ROOT),
        )

    def test_dataset_and_result_paths(self):
        """평가셋과 결과도 scope 폴더 아래 접수번호 파일명으로 분리되는지 확인한다."""

        expected_base = (
            PROJECT_ROOT / "evaluation" / "dataset" / "00126380" / "2025" / "consolidated"
        )
        self.assertEqual(self.target.dataset_path(PROJECT_ROOT), expected_base / "20260310002820.json")
        self.assertEqual(
            self.target.result_path(PROJECT_ROOT),
            PROJECT_ROOT / "evaluation" / "results" / "00126380" / "2025"
            / "consolidated" / "20260310002820.json",
        )

    # 4. 다른 기업 검색 대상 구분
    def test_different_company(self):
        other_target = SearchTarget(
            corp_code="01160363",
            fiscal_year=2025,
            statement_scope="consolidated",
            rcept_no="20260318001622",
        )

        self.assertNotEqual(
            self.target.collection_name,
            other_target.collection_name,
        )
        self.assertNotEqual(
            self.target.chunks_path(PROJECT_ROOT),
            other_target.chunks_path(PROJECT_ROOT),
        )

    # 5. 잘못된 corp_code
    def test_invalid_corp_code(self):
        for value in ["005930", "abcdefgh", "", 126380]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SearchTarget(
                        corp_code=value,
                        fiscal_year=2025,
                        statement_scope="consolidated",
                        rcept_no="20260310002820",
                    )

    # 6. 잘못된 fiscal_year
    def test_invalid_fiscal_year(self):
        for value in [0, -1, "2025", True]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SearchTarget(
                        corp_code="00126380",
                        fiscal_year=value,
                        statement_scope="consolidated",
                        rcept_no="20260310002820",
                    )

    # 7. 잘못된 statement_scope
    def test_invalid_statement_scope(self):
        for value in ["unknown", "CONSOLIDATED", "", None]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SearchTarget(
                        corp_code="00126380",
                        fiscal_year=2025,
                        statement_scope=value,
                        rcept_no="20260310002820",
                    )

    # 8. 잘못된 rcept_no
    def test_invalid_rcept_no(self):
        for value in ["12345", "abcdefghijklmn", "", 20260310002820]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SearchTarget(
                        corp_code="00126380",
                        fiscal_year=2025,
                        statement_scope="consolidated",
                        rcept_no=value,
                    )

    # 9. 검색 대상 변경 방지
    def test_immutable(self):
        with self.assertRaises(AttributeError):
            self.target.fiscal_year = 2024


if __name__ == "__main__":
    unittest.main()
