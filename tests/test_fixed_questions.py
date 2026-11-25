"""fixed_questions: 공시 용어가 중요한 주제는 brief·supervisor가 질문을 바꾸지 않는다."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research.research.nodes import supervisor
from fin_research.research.nodes.brief import BriefDraft, SubTopicDraft, build_brief, load_template
from fin_research.research.nodes.supervisor import rewrite_questions, rotate_questions
from fin_research.research.state import Brief, ClarifyResult, SubTopic

RISK_Q = ["{company}의 {years} 주석의 소송사건 건수와 소송가액은?", "{company}의 {years} 지급보증과 담보는?"]
TEMPLATE = (
    {"id": "profitability", "title": "수익성", "required_metrics": ["매출액"],
     "default_questions": ["{company}의 {years} 매출액은?", "{company}의 {years} 영업이익은?"]},
    {"id": "stability", "title": "재무 안정성", "required_metrics": ["부채비율"],
     "default_questions": ["{company}의 {years} 부채비율은?", "{company}의 {years} 차입금은?"]},
    {"id": "cashflow", "title": "현금흐름", "required_metrics": ["영업활동현금흐름"],
     "default_questions": ["{company}의 {years} 영업활동현금흐름은?", "{company}의 {years} 현금 사용처는?"]},
    {"id": "risk", "title": "리스크", "required_metrics": ["우발채무", "소송"],
     "default_questions": RISK_Q, "fixed_questions": True},
)
CLARIFY = ClarifyResult(need_clarification=False, company="NAVER", corp_code="00266961", years=[2025])


def llm_draft():
    """LLM이 리스크 질문을 흐리게 바꾼 초안 (실제 NAVER 실행에서 나온 표현)."""
    subs = [SubTopicDraft(id=s["id"], questions=[f"NAVER 2025년 {s['title']} LLM 질문1",
                                                f"NAVER 2025년 {s['title']} LLM 질문2"], metrics=[])
            for s in TEMPLATE]
    subs[-1] = SubTopicDraft(id="risk", metrics=["감사의견"], questions=[
        "NAVER의 2025년 사업보고서 주석에서 우발채무 내역이 기재된 표의 주요 항목과 금액을 알려주세요.",
        "NAVER의 2025년 리스크 요인은?"])
    return BriefDraft(objective="목적", subtopics=subs)


class TestBriefFixedQuestions(unittest.TestCase):
    def test_fixed_subtopic_ignores_llm_questions(self):
        brief = build_brief(llm_draft(), CLARIFY, TEMPLATE)
        risk = brief.subtopics[-1]
        self.assertTrue(risk.fixed_questions)
        self.assertEqual(risk.questions, ["NAVER의 2025년 주석의 소송사건 건수와 소송가액은?",
                                          "NAVER의 2025년 지급보증과 담보는?"])
        self.assertEqual(risk.metrics, ["우발채무", "소송"])  # LLM이 붙인 '감사의견'도 들어가지 않는다

    def test_other_subtopics_keep_llm_questions(self):
        brief = build_brief(llm_draft(), CLARIFY, TEMPLATE)
        self.assertFalse(brief.subtopics[0].fixed_questions)
        self.assertEqual(brief.subtopics[0].questions[0], "NAVER 2025년 수익성 LLM 질문1")

    def test_real_template_marks_risk_fixed(self):
        risk = next(s for s in load_template() if s["id"] == "risk")
        self.assertIs(risk.get("fixed_questions"), True)
        self.assertIn("소송사건", risk["default_questions"][0])


class TestSupervisorFixedQuestions(unittest.TestCase):
    def subtopics(self):
        return [SubTopic(id="stability", title="재무 안정성", questions=["A1", "A2"]),
                SubTopic(id="risk", title="리스크", questions=["R1", "R2"], fixed_questions=True)]

    def brief(self):
        return Brief(company="NAVER", corp_code="00266961", years=[2025], objective="목적",
                     subtopics=self.subtopics())

    def test_rotate(self):
        rotated = rotate_questions(SubTopic(id="risk", title="리스크", questions=["R1", "R2", "R3"],
                                            fixed_questions=True))
        self.assertEqual(rotated.questions, ["R2", "R3", "R1"])

    def test_fixed_only_does_not_call_llm(self):
        def must_not_call(*_, **__):
            raise AssertionError("고정 질문만 남았으면 LLM을 부르면 안 됩니다")
        risk = self.subtopics()[1]
        with patch.object(supervisor, "get_llm_for_role", side_effect=must_not_call):
            out, errors = asyncio.run(rewrite_questions([risk], {}, self.brief(), {}))
        self.assertEqual(out[0].questions, ["R2", "R1"])
        self.assertEqual(errors, [])

    def test_mixed_rewrites_only_unfixed(self):
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage
        import json
        response = json.dumps({"subtopics": [
            {"id": "stability", "questions": ["NAVER 새 질문1", "NAVER 새 질문2"]},
            {"id": "risk", "questions": ["LLM이 바꾼 리스크 질문1", "LLM이 바꾼 리스크 질문2"]},
        ]}, ensure_ascii=False)
        sent = []

        def fake(role, budget=None, **_):
            model = FakeMessagesListChatModel(responses=[AIMessage(response)])
            original = model.ainvoke

            async def spy(messages, *a, **k):
                sent.append(messages[-1].content)
                return await original(messages, *a, **k)
            object.__setattr__(model, "ainvoke", spy)
            return model
        with patch.object(supervisor, "get_llm_for_role", side_effect=fake):
            out, _ = asyncio.run(rewrite_questions(self.subtopics(), {}, self.brief(), {}))
        by_id = {s.id: s for s in out}
        self.assertEqual(by_id["stability"].questions, ["NAVER 새 질문1", "NAVER 새 질문2"])
        self.assertEqual(by_id["risk"].questions, ["R2", "R1"])  # LLM 답이 있어도 무시
        self.assertNotIn('"risk"', sent[0])  # 고정 주제는 LLM에 넘기지도 않는다


if __name__ == "__main__":
    unittest.main()
