"""재무비율의 단위·경계 조건·정밀도 정책 검증."""

import sys
import unittest
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fin_research.tools.ratios import (
    UndefinedRatioError,
    current_ratio,
    debt_ratio,
    operating_profit_margin,
    return_on_equity,
)


class RatioTests(unittest.TestCase):
    def test_percent_values(self):
        cases = [
            (debt_ratio, (300, 200), "150"),
            (current_ratio, (250, 100), "250"),
            (operating_profit_margin, (25, 200), "12.5"),
            # 기말자본만 쓰면 10%지만 평균자본을 쓰면 12%이다.
            (return_on_equity, (30, 200, 300), "12"),
        ]
        for function, arguments, expected in cases:
            with self.subTest(function=function.__name__):
                result = function(*arguments)
                self.assertIsInstance(result, Decimal)
                self.assertEqual(result, Decimal(expected))

    def test_zero_numerators_and_losses(self):
        self.assertEqual(debt_ratio(0, 100), Decimal(0))
        self.assertEqual(current_ratio(0, 100), Decimal(0))
        self.assertEqual(operating_profit_margin(-20, 100), Decimal(-20))
        self.assertEqual(return_on_equity(-20, 100, 100), Decimal(-20))

    def test_undefined_denominators(self):
        for denominator in (0, -100):
            for function in (debt_ratio, current_ratio, operating_profit_margin):
                with self.subTest(function=function.__name__, denominator=denominator):
                    with self.assertRaises(UndefinedRatioError):
                        function(10, denominator)
        for opening, closing in ((0, 0), (-100, 100), (-200, 100)):
            with self.subTest(opening=opening, closing=closing):
                with self.assertRaises(UndefinedRatioError):
                    return_on_equity(10, opening, closing)

    def test_negative_balance_inputs(self):
        for function in (debt_ratio, current_ratio):
            with self.assertRaises(ValueError):
                function(-1, 100)

    def test_invalid_inputs(self):
        for value in (None, True, 0.1, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    debt_ratio(value, 100)
        for value in ("", "abc", "1,000", "1_000", "10원", "NaN", "Infinity", Decimal("-Infinity")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    debt_ratio(value, 100)

    def test_roe_requires_both_equity_values(self):
        with self.assertRaises(TypeError):
            return_on_equity(10, 100)
        with self.assertRaises(TypeError):
            return_on_equity(10, None, 100)

    def test_decimal_inputs_and_unit_scale(self):
        self.assertEqual(operating_profit_margin("0.3", Decimal("2")), Decimal("15"))
        self.assertEqual(debt_ratio("3e12", "2e12"), debt_ratio(300, 200))
        self.assertEqual(return_on_equity(30, 200, 300), return_on_equity(30000, 200000, 300000))

    def test_precision_independent_of_callers_context(self):
        expected = operating_profit_margin(1, 3)
        with localcontext() as context:
            context.prec = 3
            context.rounding = ROUND_DOWN
            self.assertEqual(operating_profit_margin(1, 3), expected)
            self.assertEqual(return_on_equity(30, 200, 300), Decimal("12"))
            self.assertEqual(context.prec, 3)
            self.assertEqual(context.rounding, ROUND_DOWN)
        self.assertEqual(len(expected.as_tuple().digits), 50)


if __name__ == "__main__":
    unittest.main()
