"""상위 Researcher·실제 다중 도구 그래프·부분 재조사의 연결을 검증한다."""

import asyncio
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from langgraph.types import Command

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))

from evaluation.loop_fixture_runner import FixtureModel
from fin_research.llm.budget import Budget, BudgetExceeded
from fin_research.research import graph as outer_graph
from fin_research.research.multi_tool_adapter import resolve_context, remap_citations
from fin_research.research.nodes import supervisor
from fin_research.research.nodes.researcher import SearchToolCache, make_loop_researcher
from fin_research.research.nodes.clarify import ClarifyExtraction, validate_request
from fin_research.research.nodes.brief import build_brief, load_template
from fin_research.research.state import Brief, ClarifyResult, Finding, QuestionFinding, SubTopic
from fin_research.tools.financial_models import FinancialContext, FinancialFact

CONTEXT = FinancialContext(company="서진시스템", corp_code="00838005", year=2025,
                           scope="consolidated", rcept_no="20260323001543")
TOPIC = SubTopic(id="profitability", title="수익성", questions=["서진시스템의 2025년 연결 매출액과 영업이익률 및 감소 원인은?"])
BRIEF = Brief(company=CONTEXT.company, corp_code=CONTEXT.corp_code, years=[2025],
              objective="합성 입력으로 상위 조사 연결 평가", subtopics=[TOPIC])


def accounts():
    """실제 DB에 접근하지 않고 검수 가능한 합성 매출·영업이익을 반환한다."""
    tool = Mock()
    tool.lookup.return_value = [FinancialFact(id="sql:" + metric, metric=metric, value=Decimal(value), context=CONTEXT,
                                              account_id=metric, account_name=metric)
                                for metric, value in [("revenue", "1000000"), ("operating_profit", "200000")]]
    return tool


class FixtureSearchCache:
    """실제 임베딩 없이 첫 검색의 부족과 다음 라운드의 새 근거를 재현한다."""

    def __init__(self, *, recover=True, budget_error=False):
        """회복 여부와 예산 초과를 선택하고 세션 생성·검색 호출을 기록한다."""
        self.persist_directory = Path("unused")
        self.recover, self.budget_error = recover, budget_error
        self.sessions, self.queries = [], []

    def session(self, target, **options):
        """질문별 검색 함수를 반환하되 합성 호출 순서는 평가용 캐시에 기록한다."""
        self.sessions.append((target, options))
        def search(question, top_k):
            """다른 공시를 참조하지 않는 합성 문단을 반환한다."""
            self.queries.append(question)
            if self.budget_error:
                raise BudgetExceeded("테스트 검색 예산 초과")
            found = self.recover and len(self.queries) > 1
            return [{"chunk_id": "reason" if found else "overview", "source_file": CONTEXT.rcept_no + "_fixture.xml",
                     "source_xpath": "/P[2]" if found else "/P[1]", "pdf_page": None,
                     "text": "수요 감소로 매출 감소" if found else "기업 개요", "metadata": {}}]
        return search


def fixture_model(*, mixed=True, recover=True):
    """생산 Planner·Evaluator·Findings에 단계별 합성 응답만 주입한다."""
    calls = {"plan": 0, "evaluation": 0}
    def reply(schema):
        """첫 질문은 SQL·계산·검색, 재조사는 설명 검색만 계획한다."""
        if schema.__name__ == "ResearchPlan":
            calls["plan"] += 1
            first = calls["plan"] == 1
            return {"status": "ready", "metrics": ["revenue"] if first else [],
                    "ratios": ["operating_margin"] if first and mixed else [],
                    "document_question": ("서진시스템 매출 감소 원인" if first else "서진시스템 수요 감소 설명") if mixed else None, "reason": "테스트 계획"}
        if schema.__name__ == "EvidenceEvaluation":
            calls["evaluation"] += 1
            complete = recover and calls["evaluation"] > 1
            return {"sufficient": complete, "reason": "원인 확보" if complete else "개요만 있음",
                    "evidence_ids": ["S2" if complete else "S1"], "missing_items": [] if complete else ["매출 감소 원인"],
                    "next_query": None if complete else "서진시스템 수요 감소 설명"}
        return {"status": "draft", "answer": "회사는 수요 감소를 설명했다. [S2]",
                "evidence_ids": ["S2"], "limitations": []}
    return FixtureModel(reply)


def run_outer(node, *, brief=BRIEF, max_rounds=2, budget=None):
    """상위 그래프는 실제로 실행하고 사용자 조건·주제 생성만 고정한다."""
    async def clarify(state, config):
        """고정된 기업·단일 연도·연결 기준을 전달한다."""
        return Command(update={"clarify": ClarifyResult(need_clarification=False,
            company=brief.company, corp_code=brief.corp_code, years=brief.years, scope=brief.scope)}, goto="brief")
    async def make_brief(state, config):
        """실제 Supervisor 분배 함수를 통해 하위 조사를 시작한다."""
        return Command(update={"brief": brief, "round": 1}, goto=supervisor.dispatch(brief, brief.subtopics, 1))
    class NoWriter:
        """병합 후 그래프가 supervisor → outline → writer로 이어진다. 실제 API를 부르지 않게 빈 답(→ 조사 문장 사용)."""
        async def ainvoke(self, messages):
            from langchain_core.messages import AIMessage
            return AIMessage("")
    with patch.object(outer_graph, "clarify_node", clarify), patch.object(outer_graph, "brief_node", make_brief), \
            patch("fin_research.research.nodes.writer.get_llm_for_role", return_value=NoWriter()):
        return asyncio.run(outer_graph.build_graph(node).ainvoke({"request": "합성 테스트", "findings": []},
            outer_graph.run_config(budget, max_rounds=max_rounds, research_rcept_no=CONTEXT.rcept_no, write_report=False)))


class ResearchMultiToolTests(unittest.TestCase):
    """도구 의존성·부분 보존·기준 일치·예산·상위 종료를 검증한다."""

    def test_numeric_question_works_without_any_document_index(self):
        """SQL 질문은 색인 파일이나 임베딩 모델 없이 상위 조사에서 완료된다."""
        tool = accounts()
        node = make_loop_researcher(search_tools=SearchToolCache(Path("no-index")),
                                   account_tool=tool, model=fixture_model(mixed=False))
        result = run_outer(node)
        finding = result["findings"][0]
        self.assertEqual(finding.status, "draft")
        self.assertTrue(result["supervisor_decision"].complete)
        self.assertEqual(finding.question_results[0].numbers[0].value, "1000000")
        self.assertEqual(finding.sources[0].source_file, "fin.dart_financial_accounts")
        self.assertEqual(finding.sources[0].rcept_no, CONTEXT.rcept_no)

    def test_partial_retry_only_searches_missing_explanation_and_keeps_numbers(self):
        """SQL·계산 성공 뒤 설명만 재검색하고 수치·DB/XML 출처를 함께 보존한다."""
        tool, cache = accounts(), FixtureSearchCache()
        node = make_loop_researcher(search_tools=cache, loop_rounds=1, account_tool=tool, model=fixture_model())
        result = run_outer(node)
        finding = result["findings"][0]
        self.assertEqual(finding.status, "draft")
        self.assertEqual(finding.round, 2)
        self.assertTrue(result["supervisor_decision"].complete)
        self.assertEqual(tool.lookup.call_count, 1)
        record = finding.question_results[0]
        self.assertEqual({n.metric: Decimal(n.value) for n in record.numbers},
                         {"revenue": Decimal("1000000"), "operating_margin": Decimal("20")})
        self.assertEqual(len(record.attempts), 2)
        self.assertEqual(record.attempts[1]["tool_trace"][0]["tool"], "doc_search")
        self.assertEqual(cache.queries[1], "서진시스템 수요 감소 설명")
        self.assertTrue(any(s.source_xpath == "/P[2]" for s in finding.sources))
        self.assertIn("매출액: 1,000,000원", finding.summary)
        self.assertIn("수요 감소", finding.summary)
        self.assertNotIn("설명 부분은 필요한 근거", finding.summary)
        self.assertEqual(record.component_status["metrics"], "complete")
        self.assertEqual(record.component_status["explanation"], "complete")
        self.assertEqual(len(finding.evidence_ids), len(set(finding.evidence_ids)))

    def test_failed_second_round_keeps_confirmed_numbers_as_partial(self):
        """설명을 끝내 못 찾아도 이전 SQL·계산 결과를 잃거나 전체 완료로 처리하지 않는다."""
        tool = accounts()
        node = make_loop_researcher(search_tools=FixtureSearchCache(recover=False), loop_rounds=1,
                                   account_tool=tool, model=fixture_model(recover=False))
        result = run_outer(node)
        finding = result["findings"][0]
        self.assertEqual(finding.status, "partial")
        self.assertFalse(result["supervisor_decision"].complete)
        self.assertEqual(len(finding.question_results[0].numbers), 2)
        self.assertEqual(tool.lookup.call_count, 1)
        self.assertIn("1,000,000", finding.summary)

    def test_budget_exceeded_during_search_preserves_sql_and_stops_outer_retry(self):
        """문서 검색의 예산 초과를 일반 오류로 숨기지 않고 숫자를 남긴 채 종료한다."""
        cache = FixtureSearchCache(budget_error=True)
        node = make_loop_researcher(search_tools=cache, loop_rounds=1,
                                   account_tool=accounts(), model=fixture_model())
        result = run_outer(node)
        finding = result["findings"][0]
        self.assertEqual(finding.status, "partial")
        self.assertEqual(finding.round, 1)
        self.assertEqual(len(finding.question_results[0].numbers), 2)
        self.assertIn("예산", result["supervisor_decision"].reason)
        self.assertEqual(len(cache.queries), 1)

    def test_multi_year_request_is_not_silently_changed_to_latest_year(self):
        """지원하지 않는 다년 질문은 모델 호출 전에 보류한다."""
        loop = Mock()
        node = make_loop_researcher(tool_loop_fn=loop)
        result = run_outer(node, brief=BRIEF.model_copy(update={"years": [2024, 2025]}))
        loop.assert_not_called()
        self.assertFalse(result["supervisor_decision"].complete)
        self.assertEqual(result["round"], 1)
        self.assertIn("한 사업연도", result["findings"][0].limitations[0])

    def test_context_mismatch_cannot_publish_numbers(self):
        """도구가 다른 연도의 결과를 돌려주면 상위 답변에 숫자를 확정하지 않는다."""
        loop = Mock(return_value={"context": CONTEXT.model_copy(update={"year": 2024}).model_dump()})
        node = make_loop_researcher(tool_loop_fn=loop)
        result = run_outer(node, max_rounds=1)
        self.assertEqual(result["findings"][0].status, "insufficient_evidence")
        self.assertEqual(result["findings"][0].question_results[0].numbers, [])

    def test_question_limit_leaves_pending_questions_for_next_round(self):
        """라운드당 질문 한도 때문에 생략한 질문은 완료로 오인하지 않고 다음에 실행한다."""
        tool = accounts()
        topic = TOPIC.model_copy(update={"questions": ["첫 번째 숫자 질문", "두 번째 숫자 질문"]})
        brief = BRIEF.model_copy(update={"subtopics": [topic]})
        first_model = fixture_model(mixed=False)
        from fin_research.research.loop.tool_graph import run_tool_research
        calls = []
        def run(question, *args, **kwargs):
            """각 질문에는 독립적인 합성 계획을 주입하고 조사 순서를 기록한다."""
            calls.append(question)
            kwargs["model"] = fixture_model(mixed=False)
            return run_tool_research(question, *args, **kwargs)
        node = make_loop_researcher(questions_per_subtopic=1, tool_loop_fn=run,
                                   account_tool=tool, model=first_model)
        result = run_outer(node, brief=brief)
        self.assertEqual(calls, topic.questions)
        self.assertTrue(result["supervisor_decision"].complete)
        finding = result["findings"][0]
        self.assertEqual([r.status for r in finding.question_results], ["draft", "draft"])
        self.assertEqual(Finding.model_validate_json(finding.model_dump_json()), finding)

    def test_retry_exception_keeps_previous_numbers_and_records_attempt(self):
        """재조사 도구가 실패해도 기존 부분 결과와 실패 이력을 모두 남긴다."""
        from fin_research.research.loop.tool_graph import run_tool_research
        calls = []
        def run(question, *args, **kwargs):
            """첫 조사는 정상 실행하고 다음 조사는 실패하도록 한다."""
            calls.append(question)
            if len(calls) > 1:
                raise RuntimeError("합성 도구 실패")
            return run_tool_research(question, *args, **kwargs)
        node = make_loop_researcher(tool_loop_fn=run, loop_rounds=1,
            search_tools=FixtureSearchCache(), account_tool=accounts(), model=fixture_model())
        result = run_outer(node)
        record = result["findings"][0].question_results[0]
        self.assertEqual(record.status, "partial")
        self.assertEqual(len(record.numbers), 2)
        self.assertEqual(len(record.attempts), 2)
        self.assertEqual(record.attempts[-1]["stop_reason"], "tool_error")
        self.assertFalse(result["supervisor_decision"].complete)

    def test_scope_is_propagated_from_clarify_to_brief_and_context(self):
        """별도 요청을 연결로 바꾸지 않고 상위 계획부터 재무 기준까지 전달한다."""
        extraction = ClarifyExtraction(company_mentions=["서진시스템"], explicit_years=[2025],
                                      recent_n_years=None, focus=[], scopes=["separate"])
        clarify = validate_request(extraction)
        brief = build_brief(None, clarify, load_template())
        self.assertEqual(brief.scope, "separate")
        self.assertIn("별도 기준", brief.subtopics[0].questions[0])
        self.assertNotIn("연결 기준", brief.subtopics[0].questions[0])
        context = resolve_context(brief, rcept_no=CONTEXT.rcept_no)
        self.assertEqual(context.scope, "separate")
        extraction.scopes = ["consolidated", "separate"]
        self.assertTrue(validate_request(extraction).need_clarification)

    def test_receipt_fallback_requires_one_matching_processing_record(self):
        """기존 XML 기록의 범위를 검사하고 접수번호가 여러 개면 임의 선택하지 않는다."""
        company = {"name": CONTEXT.company, "corp_code": CONTEXT.corp_code, "reports": {"2025": {}}}
        with tempfile.TemporaryDirectory() as folder, patch("fin_research.companies.load_companies", return_value=[company]):
            root = Path(folder)
            directory = root / "data/processed/xml/dart/00838005/2025/consolidated"
            directory.mkdir(parents=True)
            for receipt in [CONTEXT.rcept_no, "20260323001544"]:
                (directory / f"{receipt}_xml_pipeline.json").write_text(json.dumps({
                    "source_format": "xml", "statement_scope": "consolidated", "source_xml": receipt + "_00761.xml"}),encoding="utf-8")
                if receipt == CONTEXT.rcept_no:
                    self.assertEqual(resolve_context(BRIEF, project_root=root), CONTEXT)
            with self.assertRaisesRegex(ValueError, "접수번호"):
                resolve_context(BRIEF, project_root=root)

    def test_grouped_citations_are_remapped_without_changing_product_names(self):
        """묶음 인용도 질문별 번호로 바꾸고 본문의 S25 제품명은 건드리지 않는다."""
        self.assertEqual(remap_citations("S25 매출 [S1, S3]", {"S1": "q1.S1", "S3": "q1.S3"}),
                         "S25 매출 [q1.S1, q1.S3]")
        with self.assertRaises(ValueError):
            remap_citations("[S9]", {"S1": "q1.S1"})

    def test_search_sessions_share_store_but_not_retry_state(self):
        """서로 다른 질문의 첫 검색이 재검색으로 잘못 시작하지 않게 상태를 분리한다."""
        created = []
        class Tool:
            """임베딩 저장소 공유와 질문별 호출 횟수를 확인하는 검색 대역."""
            def __init__(self, *args, **kwargs):
                """독립적인 검색 상태와 비어 있는 저장소를 만든다."""
                self._calls, self._vectorstore = 0, None
                created.append(self)
            def search(self, question, top_k):
                """실제 모델 없이 저장소를 준비하고 호출 횟수를 증가시킨다."""
                self._vectorstore = self._vectorstore or object()
                self._calls += 1
                return []
        cache = SearchToolCache(Path("unused"))
        # 실제 캐시 키는 불변 SearchTarget이다.
        from fin_research.retrieval.search_target import SearchTarget
        target = SearchTarget(CONTEXT.corp_code, CONTEXT.year, CONTEXT.scope, CONTEXT.rcept_no)
        with patch("fin_research.tools.doc_search_tool.XMLDocumentSearchTool", Tool):
            first, second = cache.session(target), cache.session(target)
            first("질문1", 5); first("다른 검색어", 5); second("질문2", 5)
        self.assertEqual([tool._calls for tool in created], [2, 1])
        self.assertIs(created[0]._vectorstore, created[1]._vectorstore)



class PartialFindingInReportTests(unittest.TestCase):
    """병합: 다중 도구 조사의 partial(숫자 확인, 설명 미확인)도 outline 근거로 쓰인다."""

    def test_partial_finding_is_used_by_outline(self):
        from fin_research.research.nodes.outline import build_outline
        from fin_research.research.state import Brief, Finding, SubTopic
        brief = Brief(company="NAVER", corp_code="00266961", years=[2025], objective="o",
                      subtopics=[SubTopic(id="stability", title="재무 안정성", questions=["q"])])
        partial = Finding(subtopic_id="stability", status="partial", summary="부채비율: 약 41.9% [stability.r1.q1.S1]",
                          evidence_ids=["stability.r1.q1.S1"])
        outline = build_outline({"brief": brief, "findings": [partial]})
        self.assertEqual(outline.sections[1].evidence_ids, ["stability.r1.q1.S1"])


if __name__ == "__main__":
    unittest.main()
