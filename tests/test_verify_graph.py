"""검증 그래프 조립: 노드·엣지 구조, aggregate 정렬·요약, 가짜 LLM·가짜 DB로 끝까지 실행."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research.llm.budget import Budget, BudgetCallbackHandler
from fin_research.verify.accounts import AccountStore
from fin_research.verify.graph import build_graph, run_config
from fin_research.verify.nodes import claim_extract
from fin_research.verify.nodes.aggregate import aggregate_node, order_verdicts
from fin_research.verify.state import Claim, Verdict

RCEPT = "20260313001021"


def row(sj_div, account_id, account_nm, current, ord_=1):
    return {"rcept_no": RCEPT, "fs_div": "CFS", "sj_div": sj_div, "account_id": account_id,
            "account_nm": account_nm, "account_detail": "-", "thstrm_amount": current,
            "frmtrm_amount": None, "ord": ord_}


ROWS = [
    row("CIS", "dart_OperatingIncomeLoss", "영업이익", 2_208_138_388_720.0, 5),
    row("CIS", "ifrs-full_ProfitLoss", "당기순이익", 1_818_746_310_015.0, 12),
]
PAGES = [(1, "2025년 연결 영업이익은 2.2조원이다."), (2, "당기순이익은 2.1조원이다. 직원 수는 4,000명이다.")]


class TestAggregate(unittest.TestCase):
    def test_orders_dedupes_and_fills_missing(self):
        verdicts = [Verdict(claim_id="C2", label="mismatch"), Verdict(claim_id="C1", label="match"),
                    Verdict(claim_id="C2", label="match"), Verdict(claim_id="C9", label="match")]
        ordered, notes = order_verdicts(["C1", "C2", "C3"], verdicts)
        self.assertEqual([(v.claim_id, v.label) for v in ordered],
                         [("C1", "match"), ("C2", "mismatch"), ("C3", "not_found")])
        self.assertEqual(len(notes), 2)  # C3 없음, C9 제외

    def test_node_overrides_verdicts_and_summarizes(self):
        claims = [Claim(id="C1", text="a", item="x"), Claim(id="C2", text="b", item="y")]
        out = aggregate_node({"claims": claims, "verdicts": [Verdict(claim_id="C2", label="match"),
                                                             Verdict(claim_id="C1", label="unverifiable")]})
        self.assertEqual(out["verdicts"]["type"], "override")
        self.assertEqual([v.claim_id for v in out["verdicts"]["value"]], ["C1", "C2"])
        self.assertEqual(out["summary"], {"match": 1, "mismatch": 0, "not_found": 0, "unverifiable": 1, "total": 2})
        self.assertNotIn("errors", out)


class TestGraph(unittest.TestCase):
    USAGE = {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.pages_path = Path(directory.name) / "job_pages.json"
        data = {"pages": [{"page_number": n, "text": t, "is_blank": False} for n, t in PAGES]}
        self.pages_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.fetch_calls = []

    def fake_fetch(self, corp_code, fiscal_year, fs_div):
        self.fetch_calls.append((corp_code, fiscal_year, fs_div))
        return ROWS if fs_div == "CFS" else []

    def run_graph(self, response: str, company="NAVER", budget=None):
        messages = [AIMessage(response, usage_metadata=self.USAGE)]

        def make(role, budget=None, **_):
            callbacks = [BudgetCallbackHandler(budget, role)] if budget else None
            return FakeMessagesListChatModel(responses=messages, callbacks=callbacks)

        with patch.object(claim_extract, "get_llm_for_role", side_effect=make):
            config = run_config(budget, account_store=AccountStore(fetch=self.fake_fetch))
            return asyncio.run(build_graph().ainvoke({"pdf_path": str(self.pages_path), "company": company}, config))

    def test_graph_structure(self):
        graph = build_graph().get_graph()
        self.assertEqual(set(graph.nodes) - {"__start__", "__end__"}, {"claim_extract", "verify_claim", "aggregate"})
        edges = {(e.source, e.target) for e in graph.edges}
        self.assertLessEqual({("__start__", "claim_extract"), ("verify_claim", "aggregate"),
                              ("aggregate", "__end__"), ("claim_extract", "aggregate")}, edges)

    def test_end_to_end_with_fake_llm_and_db(self):
        response = json.dumps({"claims": [
            {"text": "당기순이익은 2.1조원이다.", "item": "당기순이익", "value": 2.1, "unit": "조원", "page": 2},
            {"text": "2025년 연결 영업이익은 2.2조원이다.", "item": "영업이익", "value": 2.2, "unit": "조원",
             "year": 2025, "scope": "consolidated", "page": 1},
            {"text": "직원 수는 4,000명이다.", "item": "직원 수", "value": 4000, "unit": "명", "page": 2},
        ]}, ensure_ascii=False)
        budget = Budget()
        result = self.run_graph(response, budget=budget)

        self.assertEqual(set(result), {"company", "corp_code", "fiscal_year", "rcept_no",
                                       "claims", "verdicts", "summary", "errors"})
        self.assertEqual((result["company"], result["corp_code"], result["fiscal_year"]), ("NAVER", "00266961", 2025))
        self.assertEqual(result["rcept_no"], RCEPT)  # companies.yaml에 없으면 DB 공시 번호로 채움
        self.assertEqual([v.claim_id for v in result["verdicts"]], ["C1", "C2", "C3"])  # 주장 순서
        self.assertEqual([v.label for v in result["verdicts"]], ["mismatch", "match", "unverifiable"])
        self.assertEqual(result["summary"]["total"], 3)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.fetch_calls, [("00266961", 2025, "CFS")])  # 병렬이어도 DB 1번
        self.assertEqual(budget.summary()["by_role"]["claim_extractor"]["calls"], 1)

    def test_no_claims_still_summarizes(self):
        result = self.run_graph(json.dumps({"claims": []}))
        self.assertEqual(result["verdicts"], [])
        self.assertEqual(result["summary"]["total"], 0)

    def test_unknown_company_reports_error(self):
        result = self.run_graph(json.dumps({"claims": []}), company="애플")
        self.assertEqual(result["summary"]["total"], 0)
        self.assertIn("애플", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
