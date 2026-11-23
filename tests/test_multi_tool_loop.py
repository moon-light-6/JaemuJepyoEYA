"""다중 도구 계획·라우팅·계산·공시 계정 선택과 13개 통합 시나리오를 검증한다."""

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evaluation.integrated_fixture_runner import run_integrated_fixture
from evaluation.loop_fixture_runner import FixtureModel
from evaluation.loop_qa import LoopQACase, load_jsonl
from evaluation.metrics.loop_metrics import evaluate_case
from fin_research.research.loop.planner import ResearchPlan
from fin_research.research.loop.router import route_plan
from fin_research.research.loop.tool_graph import run_tool_research
from fin_research.retrieval.search_target import SearchTarget
from fin_research.storage.sql_store import SQLResult
from fin_research.tools.calc_tool import calculate_ratio
from fin_research.tools.doc_search_tool import XMLDocumentSearchTool
from fin_research.tools.financial_account_tool import FinancialAccountTool
from fin_research.tools.financial_models import FinancialContext, FinancialDataError, FinancialFact


CONTEXT = FinancialContext(company="평가용가상기업", corp_code="00000001", year=2025,
                           scope="consolidated", rcept_no="20260301000001")


def fact(metric, value, year=2025):
    """명확한 출처와 연도를 가진 계산용 모의 금액을 만든다."""
    return FinancialFact(id=f"{metric}:{year}", metric=metric, value=Decimal(value),
                         context=CONTEXT.model_copy(update={"year": year}))


def account_row(identifier="ifrs-full_Revenue", name="매출액", amount=100, **updates):
    """실제 OpenDART 계정 테이블의 열 구조를 갖춘 모의 행을 만든다."""
    row = {"rcept_no": CONTEXT.rcept_no, "fs_div": "CFS", "record_index": 1,
           "corp_code": CONTEXT.corp_code, "fiscal_year": 2025, "reprt_code": "11011",
           "sj_div": "CIS", "account_id": identifier, "account_nm": name,
           "currency": "KRW", "thstrm_amount": amount, "frmtrm_amount": None}
    return {**row, **updates}


class MultiToolLoopTests(unittest.TestCase):
    """생산 그래프가 여러 도구와 실패 상황을 연결하는지 확인한다."""

    def test_integration_fixtures_pass_with_production_graph(self):
        """정답은 채점기만 읽고 실제 새 그래프의 출력으로 6개 유형을 평가한다."""
        for case in load_jsonl(ROOT / "evaluation/dataset/loop_qa.jsonl", LoopQACase):
            with self.subTest(case=case.id):
                result = evaluate_case(case, run_integrated_fixture(case))
                self.assertTrue(result.passed, result.failure_types)

    def test_router_adds_formula_inputs_and_orders_dependencies(self):
        """비율 질문에 SQL 입력을 자동 보충하고 계산 전에 조회하도록 한다."""
        plan = ResearchPlan(status="ready", metrics=[], ratios=["roe", "operating_margin"],
                            document_question="평가용가상기업 원인 설명", reason="비율과 설명 필요")
        route = route_plan(plan)
        self.assertEqual(route.steps, ["sql", "calc", "doc_search"])
        self.assertTrue(route.include_prior_equity)
        self.assertEqual(set(route.lookup_metrics), {"net_income", "total_equity", "revenue", "operating_profit"})

    def test_clarification_does_not_call_any_tool(self):
        """추가 확인 계획이 나오면 DB·검색·계산을 호출하지 않는다."""
        model = FixtureModel(lambda schema: {"status": "needs_clarification", "metrics": [], "ratios": [],
                                             "document_question": None, "reason": "연간 또는 분기인지 확인 필요"})
        accounts, search = Mock(), Mock()
        result = run_tool_research("분기 실적", CONTEXT, Path("unused"), model=model,
                                   account_tool=accounts, search_function=search)
        self.assertEqual(result["stop_reason"], "needs_clarification")
        self.assertEqual(result["numbers"], [])
        accounts.lookup.assert_not_called()
        search.assert_not_called()

    def test_unknown_formula_and_empty_plan_are_rejected(self):
        """모델이 임의 수식이나 빈 작업을 지시해도 실행하지 않는다."""
        for ratios in (["eval_python"], []):
            with self.assertRaises(ValueError):
                ResearchPlan(status="ready", metrics=[], ratios=ratios, document_question=None, reason="잘못된 계획")

    def test_mixed_answer_relabels_document_citations(self):
        """SQL S1과 문서 S1이 충돌하지 않도록 전역 인용 ID를 바꾼다."""
        def reply(schema):
            """계획·근거 평가·답변의 모의 모델 응답을 분리한다."""
            if schema.__name__ == "ResearchPlan":
                return {"status": "ready", "metrics": ["revenue"], "ratios": [],
                        "document_question": "가상기업 수요 감소 원인", "reason": "숫자와 설명"}
            if schema.__name__ == "EvidenceEvaluation":
                return {"sufficient": True, "reason": "설명 확보", "evidence_ids": ["S1"],
                        "missing_items": [], "next_query": None}
            return {"status": "draft", "answer": "수요 감소라는 기업 설명이다. [S1]",
                    "evidence_ids": ["S1"], "limitations": []}
        accounts = Mock()
        accounts.lookup.return_value = [fact("revenue", "100")]
        search = Mock(return_value=[{"source_file": "fixture.xml", "source_xpath": "/P[1]", "pdf_page": None,
                                     "chunk_id": "document-reason", "text": "수요 감소", "metadata": {}}])
        result = run_tool_research("매출과 원인", CONTEXT, Path("unused"), model=FixtureModel(reply),
                                   account_tool=accounts, search_function=search)
        self.assertEqual(result["findings"]["evidence_ids"], ["S1", "S2"])
        self.assertIn("기업 설명이다. [S2]", result["findings"]["answer"])
        self.assertEqual(result["evidences"][1]["source_xpath"], "/P[1]")


class CalculationToolTests(unittest.TestCase):
    """허용 공식·기초/기말·입력 출처의 검증을 담당한다."""

    def test_four_formulas_and_average_equity(self):
        """4개 비율과 ROE의 기초·기말 평균 사용을 확인한다."""
        cases = [
            ("debt_ratio", [fact("total_liabilities", "300"), fact("total_equity", "200")], "150"),
            ("current_ratio", [fact("current_assets", "250"), fact("current_liabilities", "100")], "250"),
            ("operating_margin", [fact("revenue", "100"), fact("operating_profit", "20")], "20"),
            ("roe", [fact("net_income", "30"), fact("total_equity", "200", 2024), fact("total_equity", "300")], "12"),
        ]
        for name, facts, expected in cases:
            with self.subTest(name=name):
                result = calculate_ratio(name, facts, CONTEXT)
                self.assertEqual(result.value, Decimal(expected))
                self.assertEqual(set(result.source_ids), {item.id for item in facts})

    def test_missing_opening_equity_cannot_be_replaced_by_closing_equity(self):
        """기초 자본이 없을 때 기말 자본으로 ROE를 대신 계산하지 않는다."""
        with self.assertRaises(FinancialDataError):
            calculate_ratio("roe", [fact("net_income", "30"), fact("total_equity", "300")], CONTEXT)

    def test_unit_scope_currency_or_receipt_mismatch_is_rejected(self):
        """같은 숫자라도 서로 다른 기준의 입력을 섞지 않는다."""
        revenue = fact("revenue", "100")
        for update in ({"scope": "separate"}, {"currency": "USD"}, {"rcept_no": "20260302000001"}):
            wrong = revenue.model_copy(update={"context": revenue.context.model_copy(update=update)})
            with self.assertRaises(FinancialDataError):
                calculate_ratio("operating_margin", [wrong, fact("operating_profit", "20")], CONTEXT)
        with self.assertRaises(FinancialDataError):
            calculate_ratio("operating_margin", [revenue.model_copy(update={"unit": "백만원"}),
                                                   fact("operating_profit", "20")], CONTEXT)


class FinancialAccountTests(unittest.TestCase):
    """계정의 의미·기간·금액·중복을 검증한다."""

    def lookup(self, rows, metrics=None, **kwargs):
        """DB를 대체하되 실제 계정 선택 코드를 실행한다."""
        store = Mock()
        store.read_financial_accounts.return_value = SQLResult("fixed-query", [], rows, len(rows), False)
        return FinancialAccountTool(store).lookup(CONTEXT, metrics or ["revenue"], **kwargs)

    def test_identical_is_and_cis_values_are_not_summed(self):
        """IS와 CIS에 같은 값이 있어도 한 번만 선택한다."""
        facts = self.lookup([account_row(), account_row(sj_div="IS", record_index=2)])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].value, 100)

    def test_conflicting_duplicate_values_abstain(self):
        """동일 지표 후보의 값이 다르면 최대값 등으로 임의 선택하지 않는다."""
        with self.assertRaises(FinancialDataError) as caught:
            self.lookup([account_row(), account_row(amount=200, sj_div="IS", record_index=2)])
        self.assertEqual(caught.exception.reason, "ambiguous_data")

    def test_owners_profit_is_not_total_profit(self):
        """이름이 당기순이익이어도 지배주주순이익 ID는 전체 순이익에서 제외한다."""
        facts = self.lookup([account_row(identifier="ifrs-full_ProfitLossAttributableToOwnersOfParent",
                                        name="당기순이익")], ["net_income"])
        self.assertEqual(facts, [])

    def test_wrong_scope_currency_and_nonfinite_values_are_rejected(self):
        """실적 범위·통화가 다르거나 NaN 금액이면 정상 값으로 반환하지 않는다."""
        for row in (account_row(fs_div="OFS"), account_row(currency="USD"), account_row(amount="NaN")):
            with self.assertRaises(FinancialDataError):
                self.lookup([row])

    def test_prior_equity_is_returned_with_previous_actual_year(self):
        """같은 보고서의 전기 자본을 당기 자본과 다른 실적 연도로 기록한다."""
        rows = [account_row(identifier="ifrs-full_Equity", name="자본총계", sj_div="BS",
                            amount=300, frmtrm_amount=200)]
        facts = self.lookup(rows, ["total_equity"], include_prior_equity=True)
        self.assertEqual({item.context.year: item.value for item in facts}, {2025: 300, 2024: 200})


class XMLToolTests(unittest.TestCase):
    """XML 컬렉션·필터·출처 위치를 검사한다."""

    def test_shared_alias_accepts_spacing_but_not_another_standard_concept(self):
        """공용 별칭의 공백 정규화를 적용하되 지배주주순이익 ID는 대체하지 않는다."""
        store = Mock()
        store.read_financial_accounts.return_value = SQLResult("fixed", [], [
            account_row(identifier="-표준계정코드 미사용-", name="매출 액", amount=150),
            account_row(identifier="ifrs-full_ProfitLossAttributableToOwnersOfParent", name="당기순이익", amount=90),
        ], 2, False)
        facts = FinancialAccountTool(store).lookup(CONTEXT, ["revenue", "net_income"])
        self.assertEqual([(item.metric, item.value) for item in facts], [("revenue", 150)])
        self.assertIn("매출액", store.read_financial_accounts.call_args.args[2])

    def test_xml_search_preserves_xpath_and_selects_xml_collection(self):
        """PDF 컬렉션으로 섞이지 않고 XML 위치와 필터를 유지한다."""
        from langchain_core.documents import Document
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "chroma.sqlite3").touch()
            target = SearchTarget(CONTEXT.corp_code, 2025, "consolidated", CONTEXT.rcept_no)
            tool = XMLDocumentSearchTool(root, target, section="연결재무제표 주석")
            store = Mock()
            store._collection.count.return_value = 1
            metadata = {"chunk_id": "00000001_2025_consolidated_xml_0001_01", "source_file": "20260301000001_01.xml",
                        "source_xpath": "/DOCUMENT[1]/P[1]", "source_element_index": 1, "title": "시험 주석"}
            store.similarity_search_with_score.return_value = [(Document(page_content="설명", metadata=metadata), 0.1)]
            with patch("fin_research.retrieval.chroma_retriever.create_chroma", return_value=store) as create:
                hits = tool.search("설명 검색")
            create.assert_called_once_with(root, target.xml_collection_name)
            self.assertEqual(hits[0]["source_xpath"], "/DOCUMENT[1]/P[1]")
            self.assertIn({"section": "연결재무제표 주석"}, store.similarity_search_with_score.call_args.kwargs["filter"]["$and"])
