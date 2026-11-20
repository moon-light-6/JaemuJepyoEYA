"""brief 노드: 템플릿 조립(순서·제목·필수 지표·기본 질문)과 노드 동작(재시도·예산·실패 시 진행)을 검증합니다.

조립 규칙은 고정 fixture 템플릿으로 검증해, 평가 담당과 필수 지표를 조정해도 규칙 테스트가 흔들리지 않게 한다.
실제 configs/report_template.yaml은 형식·id 일치만 smoke 테스트한다.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research.llm.budget import Budget, BudgetCallbackHandler
from fin_research.research.nodes import brief
from fin_research.research.nodes.brief import (
    BriefDraft,
    SubTopicDraft,
    brief_node,
    build_brief,
    format_years,
    load_template,
)
from fin_research.research.state import ClarifyResult

TEMPLATE = (
    {"id": "profitability", "title": "수익성", "required_metrics": ["매출액", "영업이익"],
     "default_questions": ["{company}의 {years} 매출액은?", "{company}의 {years} 영업이익률은?"]},
    {"id": "stability", "title": "재무 안정성", "required_metrics": ["부채비율"],
     "default_questions": ["{company}의 {years} 부채비율은?", "{company}의 {years} 차입금은?"]},
    {"id": "cashflow", "title": "현금흐름", "required_metrics": ["영업활동현금흐름"],
     "default_questions": ["{company}의 {years} 영업활동현금흐름은?", "{company}의 {years} 현금 사용처는?"]},
    {"id": "risk", "title": "리스크", "required_metrics": ["감사의견"],
     "default_questions": ["{company}의 {years} 감사의견은?", "{company}의 {years} 소송은?"]},
)
TEMPLATE_IDS = [t["id"] for t in TEMPLATE]
COMPANY = "에코프로비엠"


def clarify_result(years=(2025,), focus=(), notes=()):
    return ClarifyResult(
        need_clarification=False, company=COMPANY, corp_code="01160363",
        years=list(years), focus=list(focus), notes=list(notes),
    )


def sub(id_, questions=None, metrics=()):
    return SubTopicDraft(
        id=id_, metrics=list(metrics),
        questions=questions or [f"{COMPANY}의 2025년 {id_} 질문1", f"{COMPANY}의 2025년 {id_} 질문2"],
    )


def draft(*subtopics, objective="LLM이 쓴 목적"):
    return BriefDraft(objective=objective, subtopics=list(subtopics))


class TestFormatYears(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(format_years([2025]), "2025년")
        self.assertEqual(format_years([2025, 2023, 2024]), "2023~2025년")
        self.assertEqual(format_years([2023, 2025]), "2023·2025년")
        self.assertEqual(format_years([]), "최근 사업연도")


class TestBuildBrief(unittest.TestCase):
    def build(self, d, **clarify_kwargs):
        return build_brief(d, clarify_result(**clarify_kwargs), TEMPLATE)

    def test_template_order_and_titles_regardless_of_llm_order(self):
        result = self.build(draft(sub("risk"), sub("cashflow"), sub("stability"), sub("profitability")))
        self.assertEqual([s.id for s in result.subtopics], TEMPLATE_IDS)
        self.assertEqual([s.title for s in result.subtopics], [t["title"] for t in TEMPLATE])

    def test_missing_subtopics_filled_with_default_questions(self):
        result = self.build(draft(sub("profitability")))
        self.assertEqual([s.id for s in result.subtopics], TEMPLATE_IDS)
        risk = result.subtopics[3]
        self.assertEqual(risk.questions, [f"{COMPANY}의 2025년 감사의견은?", f"{COMPANY}의 2025년 소송은?"])

    def test_duplicate_id_keeps_first(self):
        first = sub("profitability", [f"{COMPANY} 첫번째 A", f"{COMPANY} 첫번째 B"])
        second = sub("profitability", [f"{COMPANY} 두번째 A", f"{COMPANY} 두번째 B"])
        result = self.build(draft(first, second))
        self.assertEqual(result.subtopics[0].questions, [f"{COMPANY} 첫번째 A", f"{COMPANY} 첫번째 B"])

    def test_required_metrics_first_then_extra_without_duplicates(self):
        result = self.build(draft(sub("profitability", metrics=["ROE", "매출액", " ROE "])))
        self.assertEqual(result.subtopics[0].metrics, ["매출액", "영업이익", "ROE"])

    def test_company_name_added_when_missing(self):
        result = self.build(draft(sub("cashflow", ["설비투자 규모는?", f"{COMPANY}의 재고는?"])))
        self.assertEqual(result.subtopics[2].questions, [f"{COMPANY} 설비투자 규모는?", f"{COMPANY}의 재고는?"])

    def test_duplicate_questions_removed(self):
        q = f"{COMPANY}의 2025년 부채비율은?"
        result = self.build(draft(sub("stability", [q, f" {q} ", f"{COMPANY} 차입금은?"])))
        self.assertEqual(result.subtopics[1].questions, [q, f"{COMPANY} 차입금은?"])

    def test_llm_failure_uses_defaults_with_values_filled(self):
        result = self.build(None, years=(2023, 2024, 2025), focus=["현금흐름"])
        self.assertEqual([s.id for s in result.subtopics], TEMPLATE_IDS)
        for subtopic in result.subtopics:
            self.assertEqual(len(subtopic.questions), 2)
            for q in subtopic.questions:
                self.assertIn(COMPANY, q)
                self.assertIn("2023~2025년", q)
                self.assertNotIn("{", q)
        self.assertIn("현금흐름 중점", result.objective)

    def test_llm_objective_used_when_available(self):
        self.assertEqual(self.build(draft(sub("risk"))).objective, "LLM이 쓴 목적")

    def test_clarify_fields_passed_through(self):
        notes = ["2023, 2024년 자료가 없어 2025년만 분석합니다."]
        result = self.build(draft(sub("risk")), focus=["수익성"], notes=notes)
        self.assertEqual(result.company, COMPANY)
        self.assertEqual(result.corp_code, "01160363")
        self.assertEqual(result.years, [2025])
        self.assertEqual(result.focus, ["수익성"])
        self.assertEqual(result.notes, notes)

    def test_every_subtopic_contains_all_required_metrics(self):
        for d in (None, draft(sub("profitability", metrics=["ROE"]))):
            result = self.build(d)
            for subtopic, spec in zip(result.subtopics, TEMPLATE):
                self.assertEqual(subtopic.metrics[:len(spec["required_metrics"])], spec["required_metrics"])


class TestBriefDraftSchema(unittest.TestCase):
    def test_unknown_id_rejected(self):
        with self.assertRaises(ValueError):
            SubTopicDraft(id="growth", questions=["a", "b"])

    def test_question_count_bounds(self):
        with self.assertRaises(ValueError):
            SubTopicDraft(id="risk", questions=["하나뿐"])
        with self.assertRaises(ValueError):
            SubTopicDraft(id="risk", questions=["1", "2", "3", "4", "5"])


class TestBriefNode(unittest.TestCase):
    USAGE = {"input_tokens": 300, "output_tokens": 200, "total_tokens": 500}

    def setUp(self):
        patcher = patch.object(brief, "load_template", return_value=TEMPLATE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def factory(self, *responses):
        messages = [AIMessage(r, usage_metadata=self.USAGE) for r in responses]

        def make(role, budget=None, **_):
            callbacks = [BudgetCallbackHandler(budget, role)] if budget else None
            return FakeMessagesListChatModel(responses=messages, callbacks=callbacks)
        return make

    def run_node(self, state, factory, config=None):
        with patch.object(brief, "get_llm_for_role", side_effect=factory):
            return asyncio.run(brief_node(state, config or {}))

    def test_unknown_id_retried_then_built_and_budget_counted(self):
        bad = json.dumps({"objective": "x", "subtopics": [{"id": "growth", "questions": ["a", "b"], "metrics": []}]})
        good = json.dumps({"objective": "현금흐름 중점 분석", "subtopics": [
            {"id": "cashflow", "questions": [f"{COMPANY}의 2025년 영업활동현금흐름은?", "설비투자는?"],
             "metrics": ["CAPEX"]},
        ]}, ensure_ascii=False)
        budget = Budget()
        out = self.run_node({"clarify": clarify_result()}, self.factory(bad, good),
                            {"configurable": {"budget": budget}})
        self.assertEqual(sorted(out.update), ["brief", "round", "step"])
        self.assertEqual(out.update["round"], 1)
        # 하위 주제마다 researcher로 1라운드 Send
        self.assertEqual([send.node for send in out.goto], ["researcher"] * 4)
        self.assertEqual([send.arg["subtopic"].id for send in out.goto], TEMPLATE_IDS)
        self.assertEqual({send.arg["round"] for send in out.goto}, {1})
        cashflow = out.update["brief"].subtopics[2]
        self.assertEqual(cashflow.questions[1], f"{COMPANY} 설비투자는?")
        self.assertEqual(cashflow.metrics, ["영업활동현금흐름", "CAPEX"])
        self.assertEqual(budget.summary()["by_role"]["brief"]["calls"], 2)

    def test_llm_failure_continues_with_defaults_and_records_error(self):
        out = self.run_node({"clarify": clarify_result()}, self.factory("몰라", "몰라", "몰라"))
        self.assertEqual([s.id for s in out.update["brief"].subtopics], TEMPLATE_IDS)
        self.assertEqual(len(out.goto), 4)  # LLM이 실패해도 조사는 진행
        self.assertEqual(len(out.update["errors"]), 1)
        self.assertIn("기본 질문 사용", out.update["errors"][0])

    def test_requires_confirmed_clarify(self):
        def must_not_call(*_, **__):
            raise AssertionError("clarify 없이 LLM을 만들면 안 됩니다")
        unconfirmed = ClarifyResult(need_clarification=True, question="어느 기업?")
        for state in ({}, {"clarify": unconfirmed}):
            with self.assertRaises(ValueError):
                self.run_node(state, must_not_call)


class TestRealTemplate(unittest.TestCase):
    """실제 configs/report_template.yaml smoke 테스트."""

    def test_real_template_loads_and_matches_literal(self):
        template = load_template()
        self.assertEqual(sorted(t["id"] for t in template),
                         sorted(["profitability", "stability", "cashflow", "risk"]))
        for t in template:
            self.assertTrue(t["required_metrics"], t["id"])
            for q in t["default_questions"]:
                q.format(company=COMPANY, years="2025년")  # 치환 자리 오타가 있으면 KeyError

    def test_template_id_mismatch_detected_at_load(self):
        broken = {"subtopics": [dict(t) for t in TEMPLATE]}
        broken["subtopics"][0]["id"] = "growth"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "template.yaml"
            path.write_text(yaml.safe_dump(broken, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SubtopicId"):
                load_template(path)


if __name__ == "__main__":
    unittest.main()
