"""템플릿·다중 도구·공통 근거·공용 검색·부분 보고서의 병합 경계를 검증한다."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_research_multi_tool import CONTEXT, BRIEF, accounts, run_outer
from tests.test_xml_loop_search import TARGET, document
from evaluation.loop_fixture_runner import FixtureModel
from fin_research.evidence.registry import EvidenceRegistry
from fin_research.llm.budget import Budget
from fin_research.research.graph import run_config
from fin_research.research.loop.tool_graph import run_tool_research
from fin_research.research.nodes.researcher import make_loop_researcher, fetch_db_rows
from fin_research.research.nodes.outline import build_outline
from fin_research.research.nodes import writer
from fin_research.research.state import SubTopic, Finding, EvidenceRef
from fin_research.tools.doc_search_tool import XMLDocumentSearchTool


def hit(key="reason", text="수요 감소가 매출 감소 원인입니다."):
    """같은 공시의 합성 XML 원문·위치를 준비한다."""
    return {"chunk_id": key, "source_file": CONTEXT.rcept_no + "_fixture.xml",
            "source_xpath": "/P[1]", "text": text, "title": "경영진 분석", "metadata": {"source_format": "xml"}}


class SearchFixture:
    """질문별 검색 함수를 제공하고 실제 서버·임베딩 호출을 차단한다."""

    persist_directory = Path("unused")

    def __init__(self, search):
        """검색 응답과 세션 생성 횟수를 기록한다."""
        self.search, self.session_count = search, 0

    def session(self, *args, **kwargs):
        """숫자 조사에서는 이 함수를 호출해도 실제 검색은 실행되지 않는다."""
        self.session_count += 1
        return self.search


class LangchainMergeTests(unittest.TestCase):
    """합쳐진 기능의 기준·출처·완료 상태가 서로 어긋나지 않는지 검사한다."""

    def test_template_numbers_once_and_document_questions_use_same_registry(self):
        """템플릿 숫자는 한 번 조회·계산하고 설명 두 개는 SQL을 반복하지 않는다."""
        def reply(schema):
            """설명 모델만 허용하며 템플릿의 계획 생성은 코드가 담당한다."""
            if schema.__name__ == "ResearchPlan":
                raise AssertionError("템플릿 숫자·설명에는 Planner를 다시 호출하면 안 됩니다.")
            if schema.__name__ == "EvidenceEvaluation":
                return {"sufficient": True, "reason": "직접 설명", "evidence_ids": ["S1"], "missing_items": [], "next_query": None}
            return {"status": "draft", "answer": "수요 감소가 원인입니다. [S1]", "evidence_ids": ["S1"], "limitations": []}
        topic = SubTopic(id="profitability", title="수익성", questions=["매출 감소 원인", "수요 변화 원인"],
                         db_items=["revenue"], db_ratios=["operating_margin"])
        tool = accounts(); search = Mock(side_effect=[[hit()], []]); registry = EvidenceRegistry()
        node = make_loop_researcher(account_tool=tool, model=FixtureModel(reply), search_tools=SearchFixture(search))
        result = asyncio.run(node({"brief": BRIEF, "subtopic": topic, "round": 1}, run_config(None, registry)))
        finding = result["findings"][0]
        self.assertEqual(finding.status, "draft")
        self.assertEqual(tool.lookup.call_count, 1)
        self.assertEqual(search.call_count, 2)
        self.assertEqual([r.task_kind for r in finding.question_results], ["numeric", "question", "question"])
        self.assertTrue(all(registry.get(i) is not None for i in finding.evidence_ids))
        self.assertTrue(any(e.source_format == "calc" for e in registry.values()))
        self.assertEqual(len([e for e in registry.values() if e.chunk_id == "reason"]), 1)
        self.assertEqual(finding.question_results[-1].status, "draft")  # 빈 검색 응답이어도 공유한 직접 근거를 사용

    def test_retry_only_runs_document_and_does_not_duplicate_numbers(self):
        """혼합 질문의 2라운드는 설명만 재조사하고 기존 숫자를 한 번만 출력한다."""
        calls = {"plan": 0, "evaluation": 0}
        def reply(schema):
            """첫 근거는 부족하고 두 번째 근거만 충분하도록 응답한다."""
            if schema.__name__ == "ResearchPlan":
                calls["plan"] += 1
                return {"status": "ready", "metrics": ["revenue"], "ratios": [], "document_question": "최초 질문", "reason": "혼합"}
            if schema.__name__ == "EvidenceEvaluation":
                calls["evaluation"] += 1; enough = calls["evaluation"] > 1
                return {"sufficient": enough, "reason": "평가", "evidence_ids": ["S2" if enough else "S1"],
                        "missing_items": [] if enough else ["원인"], "next_query": None if enough else "새 설명 질문"}
            return {"status": "draft", "answer": "수요 감소가 원인입니다. [S2]", "evidence_ids": ["S2"], "limitations": []}
        tool = accounts(); search = Mock(side_effect=[[hit("overview", "기업 개요")], [hit()]])
        node = make_loop_researcher(account_tool=tool, model=FixtureModel(reply), search_tools=SearchFixture(search), loop_rounds=1)
        result = run_outer(node)
        finding = result["findings"][0]
        self.assertEqual(finding.status, "draft")
        self.assertEqual(calls["plan"], 1)
        self.assertEqual(tool.lookup.call_count, 1)
        self.assertEqual(finding.summary.count("매출액:"), 1)

    def test_registry_reuse_never_crosses_year_scope_or_receipt(self):
        """텍스트가 같아도 기간·기준·공시가 다르면 다른 조사의 근거를 재사용하지 않는다."""
        registry = EvidenceRegistry()
        registry.register({**hit(), "source_format": "xml", "context_key": CONTEXT.model_dump_json()})
        self.assertTrue(registry.related(CONTEXT.model_dump_json(), "매출 원인"))
        for change in ({"year": 2024}, {"scope": "separate"}, {"rcept_no": "20260323001544"}):
            self.assertEqual(registry.related(CONTEXT.model_copy(update=change).model_dump_json(), "매출 원인"), ())

    def test_unasked_fixed_question_runs_before_repeating_failed_questions(self):
        """앞의 리스크 질문이 실패해도 다음 라운드에서 아직 안 물은 감사의견을 우선 확인한다."""
        calls = []
        def loop(question, context, directory, **kwargs):
            """모든 질문이 근거 부족으로 남는 상황을 재현한다."""
            calls.append(question)
            return {"context": context.model_dump(), "findings": {"status": "insufficient_evidence",
                "answer": "근거 부족", "evidence_ids": [], "limitations": ["추가 자료 필요"]},
                "stop_reason": "max_rounds", "evidences": [], "numbers": [], "component_status": {}}
        topic = SubTopic(id="risk", title="리스크", fixed_questions=True,
                         questions=["소송", "지급보증", "감사의견"])
        brief = BRIEF.model_copy(update={"subtopics": [topic]})
        node = make_loop_researcher(tool_loop_fn=loop, questions_per_subtopic=2,
                                   search_tools=SearchFixture(Mock(return_value=[])))
        config = run_config(None)
        first = asyncio.run(node({"brief": brief, "subtopic": topic, "round": 1}, config))["findings"][0]
        asyncio.run(node({"brief": brief, "subtopic": topic, "round": 2, "previous_finding": first}, config))
        self.assertEqual(calls[2], "감사의견")

    def test_explicit_scope_does_not_fallback_to_other_scope(self):
        """구형 조회 어댑터도 연결 부재 시 별도 자료를 자동 사용하지 않는다."""
        store = Mock(); store.rows.return_value = []
        self.assertEqual(fetch_db_rows(store, "00838005", 2025, "CFS"), ([], "CFS"))
        store.rows.assert_called_once_with("00838005", 2025, "CFS")

    def test_grouped_document_citations_are_remapped_after_sql_sources(self):
        """SQL 출처가 앞에 있어도 묶음 문서 인용이 다른 출처로 바뀌지 않는다."""
        def reply(schema):
            """두 문서의 묶음 인용을 반환한다."""
            if schema.__name__ == "EvidenceEvaluation":
                return {"sufficient": True, "reason": "충분", "evidence_ids": ["S1", "S2"], "missing_items": [], "next_query": None}
            return {"status": "draft", "answer": "수요 감소 설명 [S1, S2]", "evidence_ids": ["S1", "S2"], "limitations": []}
        result = run_tool_research("매출과 설명", CONTEXT, Path("unused"), account_tool=accounts(),
            model=FixtureModel(reply), search_function=Mock(return_value=[hit("a"), hit("b")]),
            plan_override={"status": "ready", "metrics": ["revenue"], "ratios": [], "document_question": "설명", "reason": "고정"})
        self.assertIn("[S3, S4]", result["findings"]["answer"])

    def test_remote_and_local_xml_share_keyword_and_neighbor_policy(self):
        """공용 Chroma도 XML 개선 검색기를 통과하며 로컬 파일 존재를 요구하지 않는다."""
        doc = document(39, "우발채무와 소송")
        store = Mock(); store._collection.count.return_value = 1
        store._collection.get.return_value = {"documents": [doc.page_content], "metadatas": [doc.metadata]}
        store.similarity_search_with_score.return_value = [(doc, .1)]
        with tempfile.TemporaryDirectory() as folder, patch("fin_research.tools.doc_search_tool._remote_chroma", return_value=store):
            tool = XMLDocumentSearchTool(Path(folder), TARGET, client=object())
            result = tool.search("소송", 5)
            self.assertEqual(result[0]["chunk_id"], doc.metadata["chunk_id"])
            self.assertEqual(tool.last_search_info["strategy"], "hybrid_title")
            store._collection.get.assert_called_once()

    def test_budget_exhaustion_still_writes_partial_report_without_llm(self):
        """예산이 없어도 확인 숫자와 미확인 설명을 보고서에 남기고 작성 모델은 만들지 않는다."""
        finding = Finding(subtopic_id="profitability", status="partial", summary="매출액 100원 [S1]",
                          evidence_ids=["S1"], limitations=["감소 원인 미확인"], sources=[EvidenceRef(id="S1")])
        state = {"brief": BRIEF, "findings": [finding]}
        state["outline"] = build_outline(state)
        with patch.object(writer, "get_llm_for_role") as model:
            result = asyncio.run(writer.writer_node(state, run_config(Budget(max_llm_calls=0))))
        model.assert_not_called()
        self.assertIn("100원", result.update["report"])
        self.assertIn("감소 원인 미확인", result.update["report"])
        self.assertIn("일부만 확인", result.update["report"])
