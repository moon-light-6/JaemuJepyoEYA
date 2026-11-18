"""clarify 노드: 기업 매칭·연도 결정·되묻기 판단과 노드 동작(재시도·예산·라우팅)을 검증합니다.

기업 목록은 고정 fixture로 바꿔 끼워, configs/companies.yaml이 바뀌어도 규칙 테스트가 흔들리지 않게 한다.
실제 companies.yaml은 마지막 smoke 테스트에서만 읽는다.
"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.graph import END

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 노드 테스트는 가짜 LLM을 쓰지만, client 모듈 로드 시 키 검사에 걸리지 않도록 더미 키를 둔다.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research import companies as companies_module
from fin_research.llm.budget import Budget, BudgetCallbackHandler
from fin_research.research.nodes import clarify
from fin_research.research.nodes.clarify import (
    ClarifyExtraction,
    clarify_node,
    resolve_years,
    validate_request,
)

FIXTURE_COMPANIES = (
    {"name": "삼성전자", "corp_code": "00126380", "reports": {"2023": {}, "2024": {}, "2025": {}}},
    {"name": "SK하이닉스", "corp_code": "00164779", "reports": {"2025": {}}},
    {"name": "SK텔레콤", "corp_code": "00159023", "reports": {"2025": {}}},
    {"name": "에코프로비엠", "corp_code": "01160363", "reports": {"2025": {}}},
    {"name": "에코프로", "corp_code": "00536541", "reports": {"2025": {}}},
    {"name": "JYP Ent.", "corp_code": "00258689", "reports": {"2025": {}}},
    {"name": "자료없음기업", "corp_code": "99999999", "reports": {}},
)


def extraction(mentions=(), years=(), recent=None, focus=()):
    return ClarifyExtraction(
        company_mentions=list(mentions), explicit_years=list(years),
        recent_n_years=recent, focus=list(focus),
    )


class FixtureCompaniesTestCase(unittest.TestCase):
    """기업 목록을 FIXTURE_COMPANIES로 바꿔 끼운다."""

    def setUp(self):
        patcher = patch.object(companies_module, "load_companies", return_value=FIXTURE_COMPANIES)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestFindCompanies(FixtureCompaniesTestCase):
    def names(self, mention):
        return [c["name"] for c in companies_module.find_companies(mention)]

    def test_exact_match_wins_over_partial(self):
        # '에코프로'는 '에코프로비엠'의 일부지만 정확히 일치하는 쪽만 반환
        self.assertEqual(self.names("에코프로"), ["에코프로"])
        self.assertEqual(self.names("에코프로비엠"), ["에코프로비엠"])

    def test_partial_match(self):
        self.assertEqual(self.names("하이닉스"), ["SK하이닉스"])

    def test_ambiguous_partial_match(self):
        self.assertEqual(sorted(self.names("SK")), ["SK텔레콤", "SK하이닉스"])

    def test_mention_with_particle_picks_longest_name(self):
        self.assertEqual(self.names("에코프로비엠의"), ["에코프로비엠"])

    def test_legal_suffix_space_and_case_ignored(self):
        self.assertEqual(self.names("(주)JYP Ent."), ["JYP Ent."])
        self.assertEqual(self.names("jyp ent"), ["JYP Ent."])
        self.assertEqual(self.names("삼성전자㈜"), ["삼성전자"])

    def test_corp_code_match(self):
        self.assertEqual(self.names("00126380"), ["삼성전자"])

    def test_unknown_and_empty(self):
        self.assertEqual(self.names("애플"), [])
        self.assertEqual(self.names("  "), [])


class TestResolveYears(unittest.TestCase):
    """연도 규칙은 기업 목록과 무관하게 available 목록만으로 검증한다."""

    def test_recent_n_uses_latest_collected_year_not_today(self):
        years, notes, question = resolve_years(extraction(recent=2), [2023, 2024, 2025])
        self.assertEqual(years, [2024, 2025])
        self.assertEqual(notes, [])
        self.assertIsNone(question)

    def test_partial_range_continues_with_note(self):
        years, notes, question = resolve_years(extraction(recent=3), [2025])
        self.assertEqual(years, [2025])
        self.assertIn("2023, 2024년 자료가 없어", notes[0])
        self.assertIsNone(question)

    def test_explicit_years_take_priority(self):
        years, _, _ = resolve_years(extraction(years=[2024, 2024], recent=3), [2023, 2024, 2025])
        self.assertEqual(years, [2024])

    def test_no_period_means_all_collected_years(self):
        years, _, _ = resolve_years(extraction(), [2024, 2025])
        self.assertEqual(years, [2024, 2025])

    def test_out_of_range_asks(self):
        years, _, question = resolve_years(extraction(years=[2015]), [2025])
        self.assertEqual(years, [])
        self.assertIn("2015년 자료가 없습니다", question)

    def test_no_collected_reports_asks(self):
        _, _, question = resolve_years(extraction(), [])
        self.assertIn("수집된 사업보고서가 없습니다", question)

    def test_non_positive_recent_is_ignored(self):
        years, _, _ = resolve_years(extraction(recent=0), [2024, 2025])
        self.assertEqual(years, [2024, 2025])


class TestValidateRequest(FixtureCompaniesTestCase):
    def test_confirmed_request(self):
        result = validate_request(extraction(["삼성전자"], recent=2, focus=["수익성"]))
        self.assertFalse(result.need_clarification)
        self.assertEqual(result.company, "삼성전자")
        self.assertEqual(result.corp_code, "00126380")
        self.assertEqual(result.years, [2024, 2025])
        self.assertEqual(result.focus, ["수익성"])

    def test_ecopro_is_not_confused_with_ecoprobm(self):
        self.assertEqual(validate_request(extraction(["에코프로"])).company, "에코프로")
        self.assertEqual(validate_request(extraction(["에코프로비엠"])).company, "에코프로비엠")

    def test_partial_years_keep_going_with_note(self):
        result = validate_request(extraction(["에코프로비엠"], recent=3))
        self.assertFalse(result.need_clarification)
        self.assertEqual(result.years, [2025])
        self.assertEqual(len(result.notes), 1)

    def test_same_company_written_twice_is_one_company(self):
        result = validate_request(extraction(["삼성전자", "삼성전자㈜"]))
        self.assertFalse(result.need_clarification)
        self.assertEqual(result.company, "삼성전자")

    def assert_asks(self, ex, *expected_parts):
        result = validate_request(ex)
        self.assertTrue(result.need_clarification)
        self.assertIsNone(result.company)
        for part in expected_parts:
            self.assertIn(part, result.question)

    def test_no_company_asks(self):
        self.assert_asks(extraction([]), "어느 기업을 분석할까요")

    def test_unknown_company_asks(self):
        self.assert_asks(extraction(["애플"]), "'애플'", "분석 대상 기업이 아닙니다")

    def test_ambiguous_company_asks_with_candidates(self):
        self.assert_asks(extraction(["SK"]), "SK하이닉스", "SK텔레콤")

    def test_two_companies_asks(self):
        self.assert_asks(extraction(["삼성전자", "SK하이닉스"]), "한 번에 한 기업만")

    def test_out_of_range_year_asks(self):
        self.assert_asks(extraction(["삼성전자"], years=[2015]), "2015년 자료가 없습니다", "2023, 2024, 2025")

    def test_company_without_reports_asks(self):
        self.assert_asks(extraction(["자료없음기업"]), "수집된 사업보고서가 없습니다")


class TestClarifyNode(FixtureCompaniesTestCase):
    USAGE = {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70}

    def fake_llm_factory(self, *responses):
        """get_llm_for_role 대체: 정해진 응답을 돌려주고, budget이 있으면 실제 콜백으로 집계한다."""
        messages = [AIMessage(r, usage_metadata=self.USAGE) for r in responses]

        def factory(role, budget=None, **_):
            callbacks = [BudgetCallbackHandler(budget, role)] if budget else None
            return FakeMessagesListChatModel(responses=messages, callbacks=callbacks)
        return factory

    def run_node(self, request, factory, config=None):
        with patch.object(clarify, "get_llm_for_role", side_effect=factory):
            return asyncio.run(clarify_node({"request": request}, config or {}))

    def test_retries_then_confirms_and_routes_to_brief(self):
        good = json.dumps({
            "company_mentions": ["에코프로비엠"], "explicit_years": [],
            "recent_n_years": None, "focus": ["현금흐름"],
        }, ensure_ascii=False)
        budget = Budget()
        out = self.run_node(
            "에코프로비엠 현금흐름 위주로", self.fake_llm_factory("알겠습니다!", good),
            {"configurable": {"budget": budget}},
        )
        self.assertEqual(sorted(out.update), ["clarify", "step"])
        self.assertEqual(out.update["clarify"].company, "에코프로비엠")
        self.assertEqual(out.update["clarify"].focus, ["현금흐름"])
        self.assertEqual(out.goto, "brief")
        # 첫 응답(JSON 아님) + 재시도 = 2회 호출이 clarify 역할로 집계
        self.assertEqual(budget.summary()["by_role"]["clarify"]["calls"], 2)

    def test_parse_failure_asks_and_records_error(self):
        out = self.run_node("???", self.fake_llm_factory("몰라", "몰라", "몰라"))
        self.assertTrue(out.update["clarify"].need_clarification)
        self.assertEqual(len(out.update["errors"]), 1)
        self.assertEqual(out.goto, END)

    def test_empty_request_asks_without_calling_llm(self):
        def must_not_call(*_, **__):
            raise AssertionError("빈 요청에서 LLM을 만들면 안 됩니다")
        out = self.run_node("   ", must_not_call)
        self.assertTrue(out.update["clarify"].need_clarification)
        self.assertEqual(out.goto, END)


class TestRealCompaniesYaml(unittest.TestCase):
    """실제 configs/companies.yaml smoke 테스트 (규칙 자체는 위에서 fixture로 검증)."""

    def test_ecopro_pair_resolves_exactly_in_real_config(self):
        names = companies_module.supported_names()
        if not {"에코프로", "에코프로비엠"} <= set(names):
            self.skipTest("companies.yaml에 에코프로·에코프로비엠이 함께 있지 않습니다.")
        self.assertEqual([c["name"] for c in companies_module.find_companies("에코프로")], ["에코프로"])

    def test_every_company_has_name_and_corp_code(self):
        for company in companies_module.load_companies():
            self.assertTrue(company.get("name"), company)
            self.assertRegex(str(company.get("corp_code", "")), r"^\d{8}$", company.get("name"))

    def test_no_duplicate_companies(self):
        # 같은 기업이 두 번 있으면 clarify가 "HLB, HLB 중 어느 기업인가요?"처럼 되묻게 된다
        companies = companies_module.load_companies()
        for key in ("name", "corp_code"):
            values = [c[key] for c in companies]
            duplicates = sorted({v for v in values if values.count(v) > 1})
            self.assertEqual(duplicates, [], f"companies.yaml에 중복 {key}")


if __name__ == "__main__":
    unittest.main()
