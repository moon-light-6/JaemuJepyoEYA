"""XML draft 평가셋 생성의 단위 판정 테스트."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evaluation.xml_draft_dataset import (
    _detect_unit,
    build_xml_draft_dataset,
    detect_statement_unit,
)


def document(document_type: str, text: str, xpath: str = "/DOCUMENT[1]/P[1]") -> dict:
    return {
        "document_type": document_type, "text": text, "title": "t",
        "source_file": "a.xml", "source_xpath": xpath,
    }


# 본문은 '단위: 원'인데 앞쪽 주석에 '백만원'이 나오는 공시(POSCO홀딩스 유형)
WON_STATEMENT_DOCS = [
    document("xml_financial_statement", "회사 | (단위: 원)\n자 산 총 계 | 105,192,437,963,565 | 103,404,198,997,805", "/DOCUMENT[1]/TABLE[5]"),
    document("xml_note", "1. 일반사항 매출은 백만원 단위로 설명합니다.", "/DOCUMENT[1]/P[9]"),
]
ASSET_ROW = {
    "sj_div": "BS", "account_id": "ifrs-full_Assets", "account_nm": "자산총계",
    "thstrm_amount": "105,192,437,963,565", "frmtrm_amount": "103,404,198,997,805",
}


class StatementUnitTest(unittest.TestCase):
    def test_statement_unit_wins_over_note_mention(self):
        self.assertEqual(_detect_unit(WON_STATEMENT_DOCS), "백만원")  # 기존 판정(v1 생성 동작)
        self.assertEqual(detect_statement_unit(WON_STATEMENT_DOCS), "원")

    def test_falls_back_to_existing_detection_without_statement_unit(self):
        docs = [document("xml_note", "금액은 천원 단위입니다.")]
        self.assertEqual(detect_statement_unit(docs), _detect_unit(docs))

    def test_default_generation_keeps_existing_unit_behavior(self):
        items, skipped = build_xml_draft_dataset([ASSET_ROW], WON_STATEMENT_DOCS, 2025, "consolidated")
        self.assertEqual(items, [])  # 백만원으로 오판해 자산총계를 찾지 못한다(기존 동작 그대로)
        self.assertTrue(any("자산총계" in reason for reason in skipped))

    def test_explicit_unit_recovers_skipped_question(self):
        items, _ = build_xml_draft_dataset(
            [ASSET_ROW], WON_STATEMENT_DOCS, 2025, "consolidated",
            unit=detect_statement_unit(WON_STATEMENT_DOCS),
        )
        self.assertEqual(items[0]["unit"], "원")
        self.assertEqual(items[0]["expected_value"], "105,192,437,963,565")
        self.assertEqual(items[0]["gold_sources"][0]["source_xpath"], "/DOCUMENT[1]/TABLE[5]")

    def test_unknown_unit_is_rejected(self):
        with self.assertRaises(ValueError):
            build_xml_draft_dataset([ASSET_ROW], WON_STATEMENT_DOCS, 2025, "consolidated", unit="억원")


if __name__ == "__main__":
    unittest.main()
