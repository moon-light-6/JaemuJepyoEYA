"""verify/numbers.py 테스트: 금액 파싱·단위 환산·비교."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fin_research.verify.numbers import (
    compare,
    decimal_places,
    normalize_unit,
    parse_korean_amount,
    parse_number,
    rounding_tolerance,
    to_won,
)

# 실제 DB 값 (Supabase fin.dart_financial_accounts, 2025 연결)
NAVER_OPERATING_INCOME = 2_208_138_388_720
KB_ASSETS = 797_923_033_000_000


class TestNormalizeUnit(unittest.TestCase):
    def test_aliases(self):
        cases = {"조": "조원", "조 원": "조원", "억": "억원", "백만": "백만원", "KRW": "원",
                 "원": "원", "백만원": "백만원", "%": "%", "퍼센트": "%"}
        for raw, expected in cases.items():
            self.assertEqual(normalize_unit(raw), expected, raw)

    def test_unknown(self):
        for raw in (None, "", "개", "USD"):
            self.assertIsNone(normalize_unit(raw), raw)


class TestParseNumber(unittest.TestCase):
    def test_formats(self):
        cases = {"1,234": 1234.0, "△1,234": -1234.0, "▲1,234": -1234.0, "(1,234)": -1234.0,
                 "-1,234": -1234.0, "12.5%": 12.5, " 2,208,138,388,720 ": 2208138388720.0}
        for raw, expected in cases.items():
            self.assertEqual(parse_number(raw), expected, raw)

    def test_empty_or_invalid(self):
        for raw in (None, "", "-", "－", "abc", "nan", "inf"):
            self.assertIsNone(parse_number(raw), raw)


class TestParseKoreanAmount(unittest.TestCase):
    def test_mixed_units(self):
        self.assertEqual(parse_korean_amount("32조 7,260억 원"), 32_726_000_000_000)
        self.assertEqual(parse_korean_amount("2.2조원"), 2.2e12)
        self.assertEqual(parse_korean_amount("5,840억"), 584_000_000_000)
        self.assertEqual(parse_korean_amount("1조"), 10**12)

    def test_negative(self):
        self.assertEqual(parse_korean_amount("△1,345억원"), -134_500_000_000)

    def test_no_korean_unit(self):
        self.assertIsNone(parse_korean_amount("1,234"))
        self.assertIsNone(parse_korean_amount(""))


class TestToWon(unittest.TestCase):
    def test_scale(self):
        self.assertAlmostEqual(to_won(2.2, "조원"), 2.2e12)
        self.assertEqual(to_won(5840, "억"), 584_000_000_000)
        self.assertEqual(to_won(797_923_033, "백만원"), KB_ASSETS)

    def test_percent_and_unknown(self):
        self.assertEqual(to_won(12.5, "%"), 12.5)
        self.assertIsNone(to_won(100, "개"))
        self.assertIsNone(to_won(None, "조원"))


class TestPrecision(unittest.TestCase):
    def test_decimal_places(self):
        self.assertEqual(decimal_places(2.2), 1)
        self.assertEqual(decimal_places(2200.0), 0)   # JSON 정수가 float로 온 경우
        self.assertEqual(decimal_places("2.20"), 2)   # 문자열은 뒤의 0도 정밀도
        self.assertEqual(decimal_places("2,208"), 0)
        self.assertEqual(decimal_places("△1.5"), 1)

    def test_tolerance(self):
        self.assertAlmostEqual(rounding_tolerance(2.2, "조원"), 5e10)
        self.assertAlmostEqual(rounding_tolerance(120, "%"), 0.5)
        self.assertIsNone(rounding_tolerance(1, "개"))


class TestCompare(unittest.TestCase):
    def test_rounded_trillion_matches(self):
        self.assertEqual(compare(2.2, "조원", NAVER_OPERATING_INCOME).label, "match")
        self.assertEqual(compare(2.21, "조원", NAVER_OPERATING_INCOME).label, "match")
        self.assertEqual(compare(22_081, "억원", NAVER_OPERATING_INCOME).label, "match")

    def test_wrong_value_mismatches(self):
        result = compare(2.3, "조원", NAVER_OPERATING_INCOME)
        self.assertEqual(result.label, "mismatch")
        self.assertAlmostEqual(result.diff_ratio, 0.0416, places=3)

    def test_low_precision_is_lenient(self):
        # '2조'는 1.5~2.5조 범위로 본다
        self.assertEqual(compare(2, "조원", NAVER_OPERATING_INCOME).label, "match")

    def test_string_keeps_trailing_zero_precision(self):
        # '2.20조'는 2.195~2.205조 → 2.208조는 벗어난다
        self.assertEqual(compare("2.20", "조원", NAVER_OPERATING_INCOME).label, "mismatch")

    def test_exact_won(self):
        self.assertEqual(compare(797_923_033, "백만원", KB_ASSETS).label, "match")
        self.assertEqual(compare(797_923_034, "백만원", KB_ASSETS).label, "mismatch")

    def test_opposite_sign_mismatches(self):
        self.assertEqual(compare(0.3, "조원", -300_000_000_000).label, "mismatch")
        self.assertEqual(compare(-0.3, "조원", -300_000_000_000).label, "match")

    def test_percent(self):
        self.assertEqual(compare(120, "%", 119.7).label, "match")
        self.assertEqual(compare(120, "%", 118.7).label, "mismatch")

    def test_zero_db_value(self):
        result = compare(0, "원", 0)
        self.assertEqual(result.label, "match")
        self.assertIsNone(result.diff_ratio)

    def test_min_tolerance(self):
        self.assertEqual(compare("2.20", "조원", NAVER_OPERATING_INCOME, min_tolerance=1e10).label, "match")

    def test_unknown_unit_returns_none(self):
        self.assertIsNone(compare(2.2, "개", NAVER_OPERATING_INCOME))
        self.assertIsNone(compare("abc", "조원", NAVER_OPERATING_INCOME))


if __name__ == "__main__":
    unittest.main()
