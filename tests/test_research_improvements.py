"""별칭 전체 조회·혼합 요구·검색 문맥·라운드 이력·추가 원문 범위를 검증한다."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import get_args
from unittest.mock import Mock

from tests.test_multi_tool_loop import CONTEXT, account_row, fact
from tests.test_xml_loop_search import TARGET, document
from tests.ingestion.test_xml_documents import audit_xml
from evaluation.loop_fixture_runner import FixtureModel
from fin_research.ingestion.xml_documents import build_xml_documents
from fin_research.research.loop.graph import run_research_loop
from fin_research.research.loop.tool_graph import run_tool_research
from fin_research.research.nodes.brief import BriefDraft, SubTopicDraft, build_brief, load_template
from fin_research.research.state import ClarifyResult
from fin_research.storage.sql_store import SQLResult
from fin_research.tools.doc_search_tool import XMLDocumentSearchTool
from fin_research.tools.financial_account_tool import FinancialAccountTool
from fin_research.tools.financial_catalog import CATALOG
from fin_research.tools.financial_models import Metric
from fin_research.verify.accounts import load_aliases


class ResearchImprovementsTests(unittest.TestCase):
    """외부 API 없이 실제 조회 선택·그래프·XML 추출과 검색 로직을 실행한다."""

    def test_every_yaml_alias_has_a_queryable_metric_with_statement_guard(self):
        """YAML의 모든 항목을 조회하고 다른 표의 같은 이름은 선택하지 않는다."""
        expected = {"operating_profit" if key == "operating_income" else key for key in load_aliases()}
        self.assertEqual(set(get_args(Metric)), expected)
        for metric, spec in CATALOG.items():
            with self.subTest(metric=metric):
                row = account_row(identifier=spec["ids"][0], name=spec["label"],
                                  sj_div=spec["statements"][0], amount="123456789")
                store = Mock()
                store.read_financial_accounts.return_value = SQLResult(sql="fixture", columns=[], rows=[row], row_count=1, truncated=False)
                tool = FinancialAccountTool(store)
                self.assertEqual(str(tool.lookup(CONTEXT, [metric])[0].value), "123456789")
                row["sj_div"] = "SCE"
                self.assertEqual(tool.lookup(CONTEXT, [metric]), [])

    def test_mixed_unsupported_request_keeps_supported_numbers(self):
        """업계 비교가 미지원이어도 같은 질문의 매출 조회는 실행하고 부분 상태로 남긴다."""
        model = FixtureModel(lambda schema: {"status": "needs_clarification", "metrics": ["revenue"],
            "ratios": [], "document_question": None, "reason": "업계 자료 없음", "unsupported_items": ["업계 평균 비교"]})
        tool = Mock(); tool.lookup.return_value = [fact("revenue", "100")]
        result = run_tool_research("매출과 업계 평균", CONTEXT, Path("unused"), model=model, account_tool=tool)
        self.assertEqual(result["findings"]["status"], "partial")
        self.assertEqual(result["numbers"][0]["value"], "100")
        self.assertIn("미지원 요구: 업계 평균 비교", result["findings"]["limitations"])

    def test_missing_metric_does_not_discard_available_numeric_fact(self):
        """일부 지원 지표의 DB 값이 없어도 확보한 다른 지표는 유지한다."""
        model = FixtureModel(lambda schema: {"status": "ready", "metrics": ["revenue", "cash_and_equivalents"],
            "ratios": [], "document_question": None, "reason": "두 금액"})
        tool = Mock(); tool.lookup.return_value = [fact("revenue", "100")]
        result = run_tool_research("매출과 현금", CONTEXT, Path("unused"), model=model, account_tool=tool)
        self.assertEqual(result["findings"]["status"], "partial")
        self.assertEqual([n["metric"] for n in result["numbers"]], ["revenue"])
        self.assertEqual(result["stop_reason"], "missing_data")

    def test_brief_replaces_unsupported_questions_with_current_capabilities(self):
        """상위 모델이 업계·부문별·CAPEX 질문을 만들어도 기본 지원 질문으로 대체한다."""
        clarify = ClarifyResult(need_clarification=False, company="서진시스템", corp_code="00838005", years=[2025])
        draft = BriefDraft(objective="조사", subtopics=[SubTopicDraft(id="profitability",
            questions=["동종 업계와 비교해줘", "사업부문별 영업이익률 변화는?"], metrics=[])])
        brief = build_brief(draft, clarify, load_template())
        self.assertFalse(any("업계" in q or "부문별" in q for q in brief.subtopics[0].questions))
        self.assertTrue(any("매출액" in q for q in brief.subtopics[0].questions))

    def test_title_keyword_and_adjacent_chunk_recover_litigation(self):
        """벡터 검색이 표를 반환해도 제목 검색과 동일 주석 확장으로 소송 문단을 찾는다."""
        from langchain_core.documents import Document
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / "chroma.sqlite3").touch()
            tool = XMLDocumentSearchTool(root, TARGET)
            header = document(39, "보증 약정 표")
            header.metadata["title"] = "35. 우발채무 및 약정사항"
            neighbor = Document(page_content="진행중인 소송사건은 없습니다.",
                metadata={**header.metadata, "chunk_id": header.metadata["chunk_id"][:-2] + "02"})
            unrelated = document(1, "재무상태표")
            rows = [header, neighbor, unrelated]
            store = Mock(); store._collection.count.return_value = 3
            store._collection.get.return_value = {"documents": [r.page_content for r in rows], "metadatas": [r.metadata for r in rows]}
            store.similarity_search_with_score.return_value = [(unrelated, .1)]
            tool._vectorstore = store
            hits = tool.search("우발부채 및 약정", 1)
            self.assertTrue(any(h["chunk_id"] == neighbor.metadata["chunk_id"] for h in hits))
            self.assertLessEqual(len(hits), 2)
            self.assertEqual(tool.last_search_info["strategy"], "hybrid_title")

    def test_previous_round_queries_and_evidence_are_preserved(self):
        """상위 재조사에서 같은 검색어를 다시 실행하거나 이전 출처 번호를 초기화하지 않는다."""
        old = {"source_file": CONTEXT.rcept_no + "_01.xml", "source_xpath": "/P[1]",
               "chunk_id": "old", "evidence_id": "S1", "text": "기업 개요"}
        memory = {"queries": ["이미 검색"], "history": [], "evidences": [old], "evaluation": None}
        search = Mock()
        result = run_research_loop("이미 검색", Path("unused"), model=FixtureModel(lambda schema: {}),
                                   search_function=search, previous_result=memory)
        search.assert_not_called()
        self.assertEqual(result["stop_reason"], "repeated_query")
        self.assertEqual(result["evidences"], [old])

    def test_additional_audit_and_management_sections_preserve_financial_ids(self):
        """기존 재무 문서 번호는 유지하고 선택 기준 감사의견과 사업보고서 분석 절을 추가한다."""
        selected = audit_xml("연결감사보고서", "연결")
        audit_section = '<SECTION-1><TITLE>독립된 감사인의 감사보고서</TITLE><P>적정 의견 연결</P></SECTION-1>'
        separate = audit_xml("감사보고서", "").replace("<BODY>", "<BODY>" + audit_section.replace("연결", "별도"))
        business = '<DOCUMENT><DOCUMENT-NAME>사업보고서</DOCUMENT-NAME><BODY><SECTION-1><TITLE>IV. 이사의 경영진단 및 분석의견</TITLE><P>수요 감소가 원인입니다.</P></SECTION-1></BODY></DOCUMENT>'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("selected.xml", selected)
            before, _ = build_xml_documents(path, "00000001", 2025, "consolidated", "기업")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("selected.xml", selected.replace("<BODY>", "<BODY>" + audit_section))
                archive.writestr("separate.xml", separate)
                archive.writestr("business.xml", business)
            after, metadata = build_xml_documents(path, "00000001", 2025, "consolidated", "기업")
            self.assertEqual([d["page_id"] for d in before], [d["page_id"] for d in after[:len(before)]])
            self.assertEqual(metadata["research_coverage"], {"xml_audit_opinion": 1, "xml_management_analysis": 1})
            self.assertNotIn("separate.xml", {d["source_file"] for d in after})
            self.assertTrue(all(d["source_xpath"] for d in after))
