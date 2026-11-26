"""supervisor: 병렬 분배·2라운드 재조사·결과 정리와 researcher 어댑터(결과 합치기·근거 ID·도구 공유)를 검증합니다.

검색·LLM 없이 mock researcher와 가짜 루프 함수로 흐름만 확인한다.
"""

import asyncio
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research.llm.budget import Budget, BudgetExceeded
from fin_research.research import graph as graph_module
from fin_research.research.nodes import researcher as researcher_module
from fin_research.research.nodes import supervisor as supervisor_module
from fin_research.research.nodes.researcher import (
    BUDGET_EXCEEDED_MARK,
    SearchToolCache,
    make_loop_researcher,
    merge_loop_results,
    prefix_sources,
)
from fin_research.research.state import Brief, ClarifyResult, Finding, SubTopic

SUBTOPICS = [
    SubTopic(id="profitability", title="수익성", questions=["삼성전자 매출액은?", "삼성전자 영업이익률은?", "세번째?"]),
    SubTopic(id="stability", title="재무 안정성", questions=["삼성전자 부채총계는?"]),
    SubTopic(id="cashflow", title="현금흐름", questions=["삼성전자 영업활동현금흐름은?"]),
    SubTopic(id="risk", title="리스크", questions=["삼성전자 감사의견은?"]),
]
BRIEF = Brief(company="삼성전자", corp_code="00126380", years=[2025], objective="목적", subtopics=SUBTOPICS)
IDS = [s.id for s in SUBTOPICS]


def make_researcher(fail_rounds: dict[str, set[int]] | None = None, raise_for: set[str] = frozenset(),
                    tracker: dict | None = None):
    """fail_rounds[id]에 라운드가 있으면 insufficient, raise_for면 예외. tracker로 동시 실행·호출 기록."""
    fail_rounds = fail_rounds or {}
    lock = threading.Lock()

    async def researcher(inp, config):
        s, r = inp["subtopic"], inp["round"]
        if tracker is not None:
            with lock:
                tracker["running"] += 1
                tracker["max"] = max(tracker["max"], tracker["running"])
                tracker["calls"].append((s.id, r, tuple(s.questions)))
            await asyncio.sleep(0.05)
            with lock:
                tracker["running"] -= 1
        if s.id in raise_for:
            raise RuntimeError("boom")
        failed = r in fail_rounds.get(s.id, set())
        return {"findings": [Finding(
            subtopic_id=s.id, round=r,
            status="insufficient_evidence" if failed else "draft",
            summary=f"{s.id} r{r}", limitations=["근거 부족"] if failed else [],
        )]}
    return researcher


def run_graph(researcher, budget=None, rewrite_response=None, max_rounds=None):
    """clarify·brief는 고정값으로 바꿔 끼우고, supervisor LLM은 가짜 응답."""
    async def fake_clarify(state, config) -> Command:
        return Command(update={"clarify": ClarifyResult(
            need_clarification=False, company="삼성전자", corp_code="00126380", years=[2025],
        ), "step": "clarify"}, goto="brief")

    async def fake_brief(state, config) -> Command:
        return Command(update={"brief": BRIEF, "round": 1, "step": "brief"},
                       goto=supervisor_module.dispatch(BRIEF, BRIEF.subtopics, 1))

    def fake_llm(role, budget=None, **_):
        return FakeMessagesListChatModel(responses=[AIMessage(rewrite_response or "몰라")] * 3)

    configurable = {"max_rounds": max_rounds} if max_rounds else {}
    with patch.object(graph_module, "clarify_node", fake_clarify), \
            patch.object(graph_module, "brief_node", fake_brief), \
            patch.object(supervisor_module, "get_llm_for_role", side_effect=fake_llm):
        app = graph_module.build_graph(researcher)
        return asyncio.run(app.ainvoke(
            {"request": "삼성전자 분석", "findings": []},
            graph_module.run_config(budget, write_report=False, **configurable),
        ))


def by_id(out):
    return {f.subtopic_id: f for f in out["findings"]}


class TestSupervisorFlow(unittest.TestCase):
    def test_all_sufficient_finishes_in_one_round(self):
        out = run_graph(make_researcher())
        self.assertEqual(sorted(by_id(out)), sorted(IDS))
        self.assertEqual(len(out["findings"]), 4)
        self.assertTrue(out["supervisor_decision"].complete)
        self.assertEqual(out["round"], 1)

    def test_only_lacking_subtopic_is_retried_with_rewritten_questions(self):
        tracker = {"running": 0, "max": 0, "calls": []}
        rewrite = json.dumps({"subtopics": [
            {"id": "stability", "questions": ["연결 재무상태표의 부채 합계는?"]},
            {"id": "growth", "questions": ["무시되어야 함"]},
        ]}, ensure_ascii=False)
        out = run_graph(make_researcher({"stability": {1}}, tracker=tracker), rewrite_response=rewrite)

        round2 = [c for c in tracker["calls"] if c[1] == 2]
        self.assertEqual(round2, [("stability", 2, ("삼성전자 연결 재무상태표의 부채 합계는?",))])
        findings = out["findings"]
        self.assertEqual(len(findings), 4)  # 1라운드 실패 결과는 지워짐
        self.assertEqual(by_id(out)["stability"].round, 2)
        self.assertEqual(by_id(out)["stability"].status, "draft")
        self.assertEqual(out["round"], 2)
        self.assertTrue(out["supervisor_decision"].complete)

    def test_rewrite_failure_retries_with_original_questions(self):
        tracker = {"running": 0, "max": 0, "calls": []}
        out = run_graph(make_researcher({"risk": {1}}, tracker=tracker))  # 가짜 LLM이 '몰라'
        self.assertIn(("risk", 2, ("삼성전자 감사의견은?",)), tracker["calls"])
        self.assertTrue(any("질문 재작성 실패" in e for e in out.get("errors", [])))

    def test_stops_at_max_rounds_and_keeps_failed_result(self):
        out = run_graph(make_researcher({"risk": {1, 2}}))
        self.assertEqual(out["round"], 2)
        self.assertFalse(out["supervisor_decision"].complete)
        self.assertIn("최대 2라운드", out["supervisor_decision"].reason)
        self.assertEqual(len(out["findings"]), 4)
        self.assertEqual(by_id(out)["risk"].status, "insufficient_evidence")

    def test_max_rounds_one_never_retries(self):
        tracker = {"running": 0, "max": 0, "calls": []}
        out = run_graph(make_researcher({"risk": {1}}, tracker=tracker), max_rounds=1)
        self.assertEqual(len(tracker["calls"]), 4)
        self.assertFalse(out["supervisor_decision"].complete)

    def test_budget_mark_stops_retry(self):
        def researcher_factory():
            inner = make_researcher({"risk": {1}})

            async def researcher(inp, config):
                out = await inner(inp, config)
                f = out["findings"][0]
                if f.status == "insufficient_evidence":
                    f.limitations.append(f"{BUDGET_EXCEEDED_MARK} 호출 상한")
                return out
            return researcher
        out = run_graph(researcher_factory())
        self.assertEqual(out["round"], 1)
        self.assertIn("예산 소진", out["supervisor_decision"].reason)

    def test_concurrency_limited_by_config(self):
        tracker = {"running": 0, "max": 0, "calls": []}
        run_graph(make_researcher(tracker=tracker))
        self.assertLessEqual(tracker["max"], 3)  # budget.yaml max_concurrent_researchers
        self.assertGreaterEqual(tracker["max"], 2)  # 실제로 병렬 실행됨


class TestMergeAndPrefix(unittest.TestCase):
    def loop_result(self, answer, ids, status="draft", pages=None):
        pages = pages or {}
        return {
            "findings": {"status": status, "answer": answer, "evidence_ids": ids, "limitations": []},
            "evidences": [{"evidence_id": i, "pdf_page": pages.get(i), "source_file": "a.pdf",
                           "chunk_id": f"c-{i}"} for i in ids],
        }

    def test_prefix_sources(self):
        summary, ids = prefix_sources("매출 [S1], 이익 [S2].", ["S1", "S2"], "p.r1.q1")
        self.assertEqual(summary, "매출 [p.r1.q1.S1], 이익 [p.r1.q1.S2].")
        self.assertEqual(ids, ["p.r1.q1.S1", "p.r1.q1.S2"])

    def test_merge_draft_and_failed_questions(self):
        s = SUBTOPICS[0]
        f = merge_loop_results(s, 1, [
            ("q1", self.loop_result("매출 [S1]", ["S1"], pages={"S1": 85}), None),
            ("q2", {"findings": {"status": "insufficient_evidence", "limitations": ["표 없음"]}}, None),
            ("q3", None, "조사 실패: RuntimeError: x"),
        ])
        self.assertEqual(f.status, "draft")
        self.assertEqual(f.summary, "매출 [profitability.r1.q1.S1]")
        self.assertEqual(f.evidence_ids, ["profitability.r1.q1.S1"])
        self.assertEqual(f.sources[0].pdf_page, 85)
        self.assertEqual(len(f.limitations), 2)
        self.assertIn("답변 불가: 표 없음", f.limitations[0])

    def test_merge_all_failed_is_insufficient(self):
        f = merge_loop_results(SUBTOPICS[1], 2, [("q", None, "조사 실패")])
        self.assertEqual(f.status, "insufficient_evidence")
        self.assertEqual(f.round, 2)

    def test_ids_do_not_collide_across_questions(self):
        f = merge_loop_results(SUBTOPICS[0], 1, [
            ("q1", self.loop_result("a [S1]", ["S1"]), None),
            ("q2", self.loop_result("b [S1]", ["S1"]), None),
        ])
        self.assertEqual(len(set(f.evidence_ids)), 2)


# 색인 존재 확인을 건너뛰도록 "어떤 컬렉션이든 청크 1개 있음"으로 둔다 (실제 Chroma 없이 어댑터만 검사)
class _AnyIndex(dict):
    def get(self, key, default=None):
        return 1


def ANY_INDEX():
    return _AnyIndex()


class TestLoopResearcherAdapter(unittest.TestCase):
    def run_adapter(self, loop_fn, subtopic=SUBTOPICS[0], budget=None, n=2):
        cache = SearchToolCache(Path("unused"), collection_counts=ANY_INDEX)
        node = make_loop_researcher(search_tools=cache, questions_per_subtopic=n, loop_fn=loop_fn)
        inp = {"subtopic": subtopic, "brief": BRIEF, "round": 1}
        return asyncio.run(node(inp, {"configurable": {"budget": budget}})), cache

    def test_questions_limited_and_same_search_function_reused(self):
        calls = []

        def loop_fn(question, persist, top_k, model_name, rounds, *, search_function, budget, target):
            calls.append((question, search_function, target.collection_name))
            return {"findings": {"status": "draft", "answer": f"{question} [S1]", "evidence_ids": ["S1"],
                                 "limitations": []}, "evidences": [{"evidence_id": "S1"}]}

        out, _ = self.run_adapter(loop_fn, n=2)
        self.assertEqual([c[0] for c in calls], SUBTOPICS[0].questions[:2])
        self.assertIs(calls[0][1], calls[1][1])  # 같은 검색 함수 공유
        self.assertIn("00126380_2025", calls[0][2])
        self.assertEqual(out["findings"][0].status, "draft")

    def test_budget_exceeded_marks_and_stops(self):
        calls = []

        def loop_fn(question, *args, **kwargs):
            calls.append(question)
            raise BudgetExceeded("LLM 호출 상한 초과")

        out, _ = self.run_adapter(loop_fn, n=3)
        self.assertEqual(len(calls), 1)  # 첫 질문에서 멈춤
        f = out["findings"][0]
        self.assertEqual(f.status, "insufficient_evidence")
        self.assertIn(BUDGET_EXCEEDED_MARK, f.limitations[0])

    def test_missing_report_number_returns_failed_finding(self):
        # companies.yaml 내용과 무관하게, 접수번호가 없는 기업을 fixture로 둔다
        no_rcept = ({"name": "접수번호없음", "corp_code": "99999999",
                     "reports": {"2025": {"fiscal_year": 2025}}},)
        brief = BRIEF.model_copy(update={"corp_code": "99999999"})
        node = make_loop_researcher(search_tools=SearchToolCache(Path("unused"), collection_counts=ANY_INDEX), loop_fn=lambda *a, **k: {})
        with patch.object(researcher_module, "load_companies", return_value=no_rcept):
            out = asyncio.run(node({"subtopic": SUBTOPICS[0], "brief": brief, "round": 1}, {}))
        self.assertIn("접수번호", out["findings"][0].limitations[0])

    def test_search_tool_created_once_per_target_across_threads(self):
        cache = SearchToolCache(Path("unused"), collection_counts=ANY_INDEX)
        target = researcher_module.search_target_for(BRIEF)
        created = []
        with patch("fin_research.tools.doc_search_tool.DocumentSearchTool",
                   side_effect=lambda *a: created.append(a) or object()):
            fns = []
            threads = [threading.Thread(target=lambda: fns.append(cache.search_function(target)))
                       for _ in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(len(created), 1)
        self.assertEqual(len({id(f) for f in fns}), 1)


if __name__ == "__main__":
    unittest.main()
