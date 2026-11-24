"""값 근거 지표(값 Hit, 쪽+값 Hit)의 단위 테스트."""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.retrieval_metrics import (
    contains_expected_value,
    is_page_value_hit,
    is_value_hit,
    summarize_value_hits,
)


def result(chunk_id, page, text=None, source_file="a.pdf", rank=1):
    item = {
        "rank": rank,
        "chunk_id": chunk_id,
        "source_file": source_file,
        "pdf_page": page,
    }
    if text is not None:
        item["text"] = text
    return item


GOLD = [{"source_file": "a.pdf", "pdf_page": 10}]


class ContainsExpectedValueTest(unittest.TestCase):
    def test_number_must_appear(self):
        self.assertTrue(contains_expected_value("1,234,567", "자산총계 1,234,567 원"))
        self.assertFalse(contains_expected_value("1,234,567", "자산총계 7,654,321 원"))

    def test_negative_value_in_parentheses_matches_digits(self):
        self.assertTrue(contains_expected_value("(34,109,364,109)", "영업이익 (34,109,364,109)"))

    def test_all_numbers_of_a_comparison_must_be_in_the_same_text(self):
        expected = {"2025": "1,000,000", "2024": "2,000,000"}
        self.assertTrue(contains_expected_value(expected, "a 1,000,000 b 2,000,000"))
        self.assertFalse(contains_expected_value(expected, "a 1,000,000 만 있음"))

    def test_one_digit_note_number_needs_a_numbered_heading(self):
        self.assertTrue(contains_expected_value("8", "8. 매출채권 및 기타금융자산 (연결)"))
        self.assertFalse(contains_expected_value("8", "금액 8 백만원"))

    def test_sentence_needs_half_of_the_words(self):
        expected = "공정가치로 최초 인식하고 이후 재측정한다"
        self.assertTrue(contains_expected_value(expected, "공정가치로 최초 인식하며 이후 재측정"))
        self.assertFalse(contains_expected_value(expected, "전혀 상관없는 문장입니다"))

    def test_full_width_space_and_newline_are_normalised(self):
        self.assertTrue(contains_expected_value("1,000,000", "금액\n1,000,000　원"))


class ValueHitTest(unittest.TestCase):
    def test_value_hit_ignores_the_page(self):
        results = [result("c1", 99, "값 1,000,000 이 있음")]
        self.assertTrue(is_value_hit(results, "1,000,000", 3))

    def test_page_value_hit_needs_both_gold_page_and_value(self):
        results = [
            result("c1", 10, "제목만 있는 청크"),
            result("c2", 99, "값 1,000,000 이 있음", rank=2),
        ]
        self.assertTrue(is_page_value_hit(results[1:], GOLD, "1,000,000", 3) is False)
        self.assertFalse(is_page_value_hit(results, GOLD, "1,000,000", 3))

        results.append(result("c3", 10, "값 1,000,000 이 있음", rank=3))
        self.assertTrue(is_page_value_hit(results, GOLD, "1,000,000", 3))
        self.assertFalse(is_page_value_hit(results, GOLD, "1,000,000", 2))

    def test_text_is_taken_from_chunk_texts_when_missing_in_result(self):
        results = [result("c1", 10)]
        texts = {"c1": "값 1,000,000 이 있음"}
        self.assertFalse(is_value_hit(results, "1,000,000", 3))
        self.assertTrue(is_value_hit(results, "1,000,000", 3, texts))
        self.assertTrue(is_page_value_hit(results, GOLD, "1,000,000", 3, texts))

    def test_invalid_k(self):
        with self.assertRaises(ValueError):
            is_value_hit([], "1,000,000", 0)
        with self.assertRaises(ValueError):
            is_page_value_hit([], GOLD, "1,000,000", 0)


class SummarizeValueHitsTest(unittest.TestCase):
    def test_questions_without_expected_value_are_excluded(self):
        items = [
            {"value_hit_at_3": True, "value_hit_at_5": True,
             "page_value_hit_at_3": True, "page_value_hit_at_5": True},
            {"value_hit_at_3": False, "value_hit_at_5": True,
             "page_value_hit_at_3": False, "page_value_hit_at_5": False},
            {"value_hit_at_3": None, "value_hit_at_5": None,
             "page_value_hit_at_3": None, "page_value_hit_at_5": None},
        ]
        summary = summarize_value_hits(items)
        self.assertEqual(summary["value_total"], 2)
        self.assertEqual(summary["value_hit_at_3"], 0.5)
        self.assertEqual(summary["value_hit_at_5"], 1.0)
        self.assertEqual(summary["page_value_hit_at_5"], 0.5)
        self.assertEqual(summary["page_value_hits_at_3"], 1)

    def test_no_valued_questions_gives_none(self):
        items = [{"value_hit_at_3": None, "value_hit_at_5": None,
                  "page_value_hit_at_3": None, "page_value_hit_at_5": None}]
        summary = summarize_value_hits(items)
        self.assertEqual(summary["value_total"], 0)
        self.assertIsNone(summary["page_value_hit_at_3"])


if __name__ == "__main__":
    unittest.main()
