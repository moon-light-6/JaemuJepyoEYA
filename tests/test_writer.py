"""outline·writer: 목차(코드)와 섹션 작성, 숫자·출처 검사, 대체 동작, 그래프 끝까지 (가짜 LLM)."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research.llm.budget import BudgetExceeded
from fin_research.research.nodes import writer
from fin_research.research.nodes.outline import build_outline, outline_node
from fin_research.research.nodes.writer import (
    check_section,
    citations_in,
    insufficient_text,
    numbers_in,
    section_source,
    writer_node,
)
from fin_research.research.state import Brief, EvidenceRef, Finding, SubTopic

SUBTOPICS = [SubTopic(id=i, title=t, questions=["q1", "q2"]) for i, t in
             [("profitability", "수익성"), ("stability", "재무 안정성"), ("cashflow", "현금흐름"), ("risk", "리스크")]]
BRIEF = Brief(company="NAVER", corp_code="00266961", years=[2025], objective="목적", subtopics=SUBTOPICS)
FINDINGS = [
    Finding(subtopic_id="profitability", status="draft",
            summary="매출액은 12,035,007,218,975원, 영업이익은 2,208,138,388,720원입니다. [S2]",
            evidence_ids=["S2"], sources=[EvidenceRef(id="S2", quote="I. 영업수익 | 35 | 12,035,007,218,975")]),
    Finding(subtopic_id="stability", status="insufficient_evidence", summary="x", limitations=["부채총계 근거 없음"]),
    Finding(subtopic_id="cashflow", status="draft", summary="영업활동현금흐름은 3,095,806,604,677원입니다. [S4]",
            evidence_ids=["S4"]),
    Finding(subtopic_id="risk", status="draft", summary="소송 49건 피고, 소송가액 8,947백만원입니다. [S1]",
            evidence_ids=["S1"]),
]


def state():
    return {"brief": BRIEF, "findings": FINDINGS, "outline": build_outline({"brief": BRIEF, "findings": FINDINGS})}


def run_writer(responses, error=None):
    """writer LLM을 가짜로. 섹션 제목별로 정해 둔 답을 돌려준다 (동시 실행이라 순서에 기대지 않는다)."""
    calls = []

    class FakeWriter:
        async def ainvoke(self, messages):
            title = messages[-1].content.split("섹션: ")[1].split("\n")[0]
            calls.append(title)
            if error:
                raise error
            return AIMessage(responses[title])

    with patch.object(writer, "get_llm_for_role", return_value=FakeWriter()), \
            patch.object(writer, "load_prompt", return_value="SYSTEM"):
        out = asyncio.run(writer_node(state(), {}))
    return out, calls


def section(report, title):
    return report.split(f"## {title}\n\n")[1].split("\n\n## ")[0].strip()


GOOD = {
    "수익성": "매출액 12,035,007,218,975원, 영업이익 2,208,138,388,720원으로 수익성이 견조합니다. [S2]",
    "현금흐름": "영업활동현금흐름 3,095,806,604,677원으로 현금 창출력이 양호합니다. [S4]",
    "리스크": "소송 49건(소송가액 8,947백만원)이 계류 중입니다. [S1]",
    "요약": "매출액 12,035,007,218,975원을 기록했고 [S2], 소송 49건이 있습니다 [S1].",
    "결론": "수익성과 현금 창출력이 양호하나 계류 중인 소송을 지켜볼 필요가 있습니다.",
}


class TestOutline(unittest.TestCase):
    def test_sections_in_report_order(self):
        outline = build_outline({"brief": BRIEF, "findings": FINDINGS})
        self.assertEqual([s.title for s in outline.sections],
                         ["요약", "수익성", "재무 안정성", "현금흐름", "리스크", "결론"])

    def test_failed_subtopic_has_no_evidence(self):
        by_title = {s.title: s for s in build_outline({"brief": BRIEF, "findings": FINDINGS}).sections}
        self.assertEqual(by_title["재무 안정성"].evidence_ids, [])
        self.assertEqual(by_title["요약"].subtopic_ids, ["profitability", "cashflow", "risk"])
        self.assertEqual(set(by_title["결론"].evidence_ids), {"S1", "S2", "S4"})

    def test_latest_finding_per_subtopic(self):
        retry = Finding(subtopic_id="stability", status="draft", summary="부채비율 41.9% [S7]",
                        evidence_ids=["S7"], round=2)
        outline = build_outline({"brief": BRIEF, "findings": [*FINDINGS, retry]})
        self.assertEqual(outline.sections[2].evidence_ids, ["S7"])

    def test_node_goes_to_writer(self):
        out = outline_node({"brief": BRIEF, "findings": FINDINGS})
        self.assertEqual(out.goto, "writer")
        self.assertEqual(out.update["step"], "outline")


class TestChecks(unittest.TestCase):
    def test_numbers_in(self):
        self.assertEqual(numbers_in("매출 12,035억, 이익 2,208억, 49건, 3개 주제"), {"12035", "2208", "49"})

    def test_citations_in_reads_groups_not_product_names(self):
        self.assertEqual(citations_in("갤럭시 S25 판매 [S1, S3]. 또한 [S4]"), {"S1", "S3", "S4"})

    def test_check_passes_copied_numbers(self):
        source = "영업이익은 2,208,138,388,720원입니다. [S2]"
        self.assertEqual(check_section("영업이익 2,208,138,388,720원 [S2]", source, {"S2"}), [])

    def test_check_catches_new_number_unit_change_and_bad_source(self):
        source = "영업이익은 2,208,138,388,720원입니다. [S2]"
        self.assertIn("입력에 없는 숫자", check_section("영업이익 약 2.21조원 [S2]", source, {"S2"})[0])
        self.assertIn("입력에 없는 출처", check_section("영업이익 2,208,138,388,720원 [S9]", source, {"S2"})[0])
        self.assertIn("빈 답변", check_section("  ", source, {"S2"})[0])

    def test_section_source_includes_quotes_and_limitations(self):
        finding = FINDINGS[0].model_copy(update={"limitations": ["당기순이익 미확인"]})
        text = section_source([finding])
        self.assertIn("[조사 결과]", text)
        self.assertIn("S2: I. 영업수익", text)
        self.assertIn("- 당기순이익 미확인", text)

    def test_insufficient_text(self):
        self.assertIn("부채총계 근거 없음", insufficient_text([FINDINGS[1]]))


class TestWriterNode(unittest.TestCase):
    def test_good_answers_are_used(self):
        out, calls = run_writer(GOOD)
        report = out.update["report"]
        self.assertTrue(report.startswith("# NAVER 재무 분석 보고서 (2025년)"))
        self.assertEqual(section(report, "수익성"), GOOD["수익성"])
        self.assertEqual(section(report, "결론"), GOOD["결론"])
        self.assertNotIn("errors", out.update)
        self.assertEqual(out.goto, "__end__")

    def test_section_without_evidence_does_not_call_llm(self):
        out, calls = run_writer(GOOD)
        self.assertNotIn("재무 안정성", calls)
        self.assertEqual(len(calls), 5)  # 주제 3 + 요약 + 결론
        self.assertIn("확인하지 못했습니다", section(out.update["report"], "재무 안정성"))

    def test_invented_number_falls_back_to_finding(self):
        answers = {**GOOD, "현금흐름": "영업활동으로 약 3.1조원을 벌었습니다. [S4]"}
        out, _ = run_writer(answers)
        self.assertEqual(section(out.update["report"], "현금흐름"), FINDINGS[2].summary)
        self.assertIn("입력에 없는 숫자", out.update["errors"][0])

    def test_unknown_source_in_conclusion_falls_back(self):
        out, _ = run_writer({**GOOD, "결론": "수익성이 양호합니다. [S9]"})
        self.assertNotIn("[S9]", out.update["report"])
        self.assertTrue(any("S9" in e for e in out.update["errors"]))

    def test_summary_cannot_use_numbers_rejected_in_body(self):
        # 본문에서 걸러진 '3.1'은 요약 입력에도 없으므로 요약에 쓰면 걸린다
        answers = {**GOOD, "현금흐름": "약 3.1조원 [S4]", "요약": "현금흐름 약 3.1조원 [S4]."}
        out, _ = run_writer(answers)
        self.assertNotIn("3.1조", section(out.update["report"], "요약"))

    def test_budget_exceeded_keeps_report(self):
        out, _ = run_writer(GOOD, error=BudgetExceeded("한도 초과"))
        self.assertEqual(section(out.update["report"], "수익성"), FINDINGS[0].summary)
        self.assertTrue(all("[예산 초과]" in e for e in out.update["errors"]))

    def test_nothing_found(self):
        empty = [f.model_copy(update={"status": "insufficient_evidence", "evidence_ids": []}) for f in FINDINGS]
        st = {"brief": BRIEF, "findings": empty, "outline": build_outline({"brief": BRIEF, "findings": empty})}
        with patch.object(writer, "get_llm_for_role", return_value=object()), \
                patch.object(writer, "load_prompt", return_value="SYSTEM"):
            out = asyncio.run(writer_node(st, {}))
        self.assertIn("작성하지 못했습니다", section(out.update["report"], "요약"))


class TestGraphEndToEnd(unittest.TestCase):
    def test_research_graph_ends_with_written_report(self):
        from langgraph.types import Command

        from fin_research.research import graph as graph_module
        from fin_research.research.nodes import supervisor as supervisor_module
        from fin_research.research.state import ClarifyResult

        async def fake_clarify(state, config):
            return Command(update={"clarify": ClarifyResult(need_clarification=False, company="NAVER",
                                                            corp_code="00266961", years=[2025])}, goto="brief")

        async def fake_brief(state, config):
            return Command(update={"brief": BRIEF, "round": 1},
                           goto=supervisor_module.dispatch(BRIEF, BRIEF.subtopics, 1))

        by_id = {f.subtopic_id: f for f in FINDINGS if f.status == "draft"}

        async def fake_researcher(inp, config):
            s = inp["subtopic"]
            found = by_id.get(s.id) or Finding(subtopic_id=s.id, status="insufficient_evidence", summary="없음")
            return {"findings": [found]}

        class FakeWriter:
            async def ainvoke(self, messages):
                title = messages[-1].content.split("섹션: ")[1].split("\n")[0]
                return AIMessage(GOOD[title])

        with patch.object(graph_module, "clarify_node", fake_clarify), \
                patch.object(graph_module, "brief_node", fake_brief), \
                patch.object(supervisor_module, "get_llm_for_role",
                             return_value=FakeMessagesListChatModel(responses=[AIMessage("몰라")] * 3)), \
                patch.object(writer, "get_llm_for_role", return_value=FakeWriter()), \
                patch.object(writer, "load_prompt", return_value="SYSTEM"):
            out = asyncio.run(graph_module.build_graph(fake_researcher).ainvoke(
                {"request": "NAVER 분석", "findings": []}, graph_module.run_config(None, max_rounds=1)))
        self.assertEqual([s.title for s in out["outline"].sections][0], "요약")
        self.assertIn("## 결론", out["report"])
        self.assertIn(GOOD["리스크"], out["report"])


if __name__ == "__main__":
    unittest.main()
