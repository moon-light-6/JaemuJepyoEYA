"""2단계 근거: 검색된 청크에서 짧은 조각을 고른다 (실제 NAVER 청크 모양을 줄여 옮긴 fixture)."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research.retrieval.span_select import score_span, select_quote, split_header, split_spans

NOTE = (
    "32. 우발채무 및 약정사항가. 당기말 현재 연결회사는 국민은행 외 8개 금융기관과 한도 668,000백만원의 "
    "대출약정을 체결 중입니다.나. 당기말 현재 연결회사는 서울보증보험 등으로 부터 174,800백만원의 보증을 "
    "제공받고 있습니다. 또한 출자금 3,595백만원을 담보로 제공하고 있습니다.다. 당기말 현재 연결회사는 고객의 "
    "선불충전금 385,584백만원을 신탁하고 있습니다.사. 당기말 현재 연결회사는 특허권 침해 및 손해배상 등과 "
    "관련하여 49건의 소송사건에 피고로 계류 중에 있으며(소송가액: 8,947백만원), 손해배상 등과 관련하여 "
    "13의 소송사건에 원고로 계류 중에 있습니다(소송가액: 3,803백만원). 다만 최종 결과는 알 수 없습니다."
)
TABLE = (
    "[NAVER | 2025년 연결 | 재무상태표 | 단위: 원 | 열: 구 분, 주석, 제27기(2025년), 제26기(2024년)]\n"
    "연결재무상태표\n구 분 | 주석 | 제 27 (당) 기말 | 제 26 (전) 기말\n자 산\n"
    "l. 유동자산 | 10,928,462,735,839 | 9,374,912,034,828\n"
    "현금및현금성자산 | 6,7 | 5,984,018,799,741 | 4,195,524,637,851\n"
    "단기금융상품 | 6,7 | 2,336,927,834,770 | 2,859,768,247,306\n"
    "재고자산 | 9 | 20,235,420,058 | 21,743,796,031"
)
LAWSUIT_Q = "NAVER의 2025년 연결재무제표 주석에서 우발채무 및 약정사항으로 공시된 소송사건의 건수와 소송가액은 얼마인가?"


class TestSplit(unittest.TestCase):
    def test_note_items_split_on_korean_markers(self):
        spans = split_spans(NOTE)
        self.assertEqual([s.kind for s in spans], ["item"] * 4)
        self.assertTrue(spans[3].text.startswith("사. 당기말"))
        self.assertIn("다만 최종 결과는", spans[3].text)  # '다만'은 항목 기호가 아니다

    def test_table_rows_keep_header_and_unit(self):
        spans = split_spans(TABLE)
        rows = [s for s in spans if s.kind == "row"]
        self.assertEqual(len(rows), 4)  # 숫자가 있는 행만
        cash = next(s for s in rows if "현금및현금성자산" in s.text)
        self.assertIn("단위: 원", cash.text)
        self.assertIn("제27기(2025년)", cash.text)

    def test_header_detection(self):
        header, body = split_header(TABLE)
        self.assertTrue(header.startswith("[NAVER |"))
        self.assertTrue(body.strip().startswith("연결재무상태표"))
        # 실제 청크처럼 머리글 안에 본문이 길게 갇힌 경우 (']'가 본문 끝에 있음)
        unclosed = "[검색 문맥 | 연결재무제표 주석 | " + NOTE[:200] + "]"
        self.assertTrue(split_header(unclosed)[1].startswith("32. 우발채무"))

    def test_sentences_when_no_markers(self):
        text = ("당기말 현재 연결회사는 국민은행 외 8개 금융기관과 대출약정을 체결 중입니다. "
                "또한 연결회사는 서울보증보험 등으로부터 이행보증과 지급보증을 제공받고 있습니다.")
        self.assertEqual([s.kind for s in split_spans(text)], ["sentence", "sentence"])


class TestSelect(unittest.TestCase):
    def test_lawsuit_question_picks_item_sa(self):
        answer = "49건의 소송사건에 피고로 계류 중이며 소송가액은 8,947백만원입니다. [S1]"
        quote = select_quote(NOTE, LAWSUIT_Q, answer)
        self.assertTrue(quote.startswith("사. 당기말"))
        self.assertIn("8,947백만원", quote)

    def test_guarantee_question_picks_item_na(self):
        quote = select_quote(NOTE, "NAVER의 지급보증과 담보로 제공한 자산은?", "출자금 3,595백만원을 담보로 제공 [S1]")
        self.assertTrue(quote.startswith("나. 당기말"))

    def test_table_row_by_answer_number(self):
        quote = select_quote(TABLE, "NAVER 2025년 현금및현금성자산은?", "5,984,018,799,741원입니다. [S1]")
        self.assertTrue(quote.endswith("현금및현금성자산 | 6,7 | 5,984,018,799,741 | 4,195,524,637,851"))

    def test_quote_is_original_text(self):
        # 조각은 원문을 자르기만 한다 (공백 정리 외에 글자를 바꾸지 않는다)
        quote = select_quote(NOTE, LAWSUIT_Q, "8,947백만원")
        self.assertIn(" ".join(quote.split()), " ".join(NOTE.split()))

    def test_short_chunk_needs_no_quote(self):
        self.assertIsNone(select_quote("현금및현금성자산 | 5,984,018,799,741", "현금은?", "5,984,018,799,741"))

    def test_answer_number_beats_word_overlap(self):
        # 질문 단어가 많이 겹치는 조각보다 답변 숫자가 있는 조각이 이긴다
        self.assertGreater(score_span("소송가액: 8,947백만원", "소송", "8,947백만원"),
                           score_span("소송사건 소송가액 소송 계류 소송", "소송사건 소송가액", "8,947백만원"))

    def test_question_year_not_counted_as_answer_number(self):
        a = score_span("2025년 현금 5,984", "2025년 현금은?", "2025년 5,984")
        b = score_span("2025년 재고 20,235", "2025년 현금은?", "2025년 5,984")
        self.assertGreater(a, b)


class TestResearcherQuote(unittest.TestCase):
    def test_merge_attaches_quote_to_source(self):
        from fin_research.evidence.registry import EvidenceRegistry
        from fin_research.research.nodes.researcher import merge_loop_results
        from fin_research.research.state import SubTopic

        evidence = {"evidence_id": "S1", "source_file": "a.xml", "chunk_id": "note-32", "text": NOTE,
                    "pdf_page": None, "metadata": {"source_xpath": "/D[1]/P[439]", "title": "32. 우발채무…"}}
        result = {"findings": {"status": "draft", "evidence_ids": ["S1"], "limitations": [],
                               "answer": "49건 피고, 소송가액 8,947백만원 [S1]"}, "evidences": [evidence]}
        finding = merge_loop_results(SubTopic(id="risk", title="리스크", questions=[LAWSUIT_Q, "q2"]), 1,
                                     [(LAWSUIT_Q, result, None)], EvidenceRegistry())
        self.assertTrue(finding.sources[0].quote.startswith("사. 당기말"))


class TestDemoDisplay(unittest.TestCase):
    def test_long_title_shortened(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "demo"))
        import demo_research
        from fin_research.research.state import EvidenceRef

        ref = EvidenceRef(id="S1", source_file="a.xml", source_xpath="/D[1]", title=NOTE)
        text = demo_research.describe_location(ref)
        self.assertLess(len(text), 70)
        self.assertTrue(text.startswith("32. 우발채무 및 약정사항가."))


if __name__ == "__main__":
    unittest.main()
