"""Hybrid RAG: DB 숫자 문장(db_facts), 근거 원문에 없는 숫자 제거(text_checks), researcher 합치기. DB·LLM 없음."""

import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research.evidence.models import EvidenceCandidate
from fin_research.evidence.registry import EvidenceRegistry
from fin_research.research.db_facts import build_db_facts, percent, topic, won
from fin_research.research.nodes.researcher import (
    collect_db_facts,
    db_spec,
    failed_finding,
    merge_loop_results,
    with_db_facts,
)
from fin_research.research.state import Brief, SubTopic
from fin_research.research.text_checks import drop_unsupported_numbers, numbers_in, split_sentences
from decimal import Decimal

RCEPT = "20260313001021"


def row(sj, account_id, name, cur, prior, ord_=1):
    return {"rcept_no": RCEPT, "fs_div": "CFS", "sj_div": sj, "account_id": account_id, "account_nm": name,
            "account_detail": None, "thstrm_amount": cur, "frmtrm_amount": prior, "ord": ord_}


ROWS = [
    row("CIS", "ifrs-full_Revenue", "영업수익", 12035007218975.0, 10737719264647.0),
    row("CIS", "dart_OperatingIncomeLoss", "영업이익", 2208138388720.0, 1979262410580.0),
    row("CIS", "ifrs-full_ProfitLoss", "당기순이익", 1953214734956.0, 1888800000000.0),
    # 자본변동표의 당기순이익은 고르지 않아야 한다
    row("SCE", "ifrs-full_ProfitLoss", "당기순이익", 1923237257015.0, None, 0),
    row("BS", "ifrs-full_Equity", "자본총계", 30000000000000.0, 28000000000000.0),
    row("BS", "ifrs-full_Liabilities", "부채총계", 12000000000000.0, 11200000000000.0),
    row("CF", "ifrs-full_CashFlowsFromUsedInInvestingActivities", "투자활동현금흐름", -743677268012.0, None),
]


def build(items, ratio_keys, rows=ROWS):
    registry = EvidenceRegistry()
    facts = build_db_facts(rows, items=items, ratio_keys=ratio_keys, company="NAVER", corp_code="00266961",
                           year=2025, fs_div="CFS", subtopic_id="test", register=registry.register)
    return facts, registry


class TestFormat(unittest.TestCase):
    def test_won_and_percent(self):
        self.assertEqual(won(-743677268012.0), "-743,677,268,012원")
        self.assertEqual(percent(Decimal("18.3477")), "18.3%")
        self.assertEqual(percent(Decimal("40.05")), "40.1%")  # 반올림은 HALF_UP

    def test_topic_particle(self):
        self.assertEqual(topic("매출액"), "매출액은")
        self.assertEqual(topic("부채총계"), "부채총계는")
        self.assertEqual(topic("자기자본이익률(ROE)"), "자기자본이익률(ROE)은")


class TestBuildDbFacts(unittest.TestCase):
    def test_amounts_from_main_statement_with_prior(self):
        facts, registry = build(["revenue", "operating_income", "net_income"], [])
        self.assertIn("매출액은 12,035,007,218,975원(전기 10,737,719,264,647원)", facts.summary)
        self.assertIn("당기순이익은 1,953,214,734,956원", facts.summary)  # 자본변동표 값(1,923…) 아님
        self.assertNotIn("1,923,237,257,015", facts.summary)
        self.assertEqual(facts.evidence_ids, ["S1"])
        evidence = registry.require("S1")
        self.assertEqual(evidence.source_format, "db")
        self.assertIn("접수번호 20260313001021", evidence.source_file)

    def test_ratios_are_computed_by_code(self):
        facts, registry = build(["revenue", "operating_income"], ["operating_profit_margin"])
        self.assertIn("영업이익률은 18.3%(전기 18.4%)입니다. [S2]", facts.summary)
        calc = registry.require("S2")
        self.assertEqual(calc.source_format, "calc")
        self.assertIn("2,208,138,388,720원 / 12,035,007,218,975원 × 100 = 18.3%", calc.text)

    def test_roe_uses_opening_and_closing_equity(self):
        facts, _ = build([], ["return_on_equity"])
        # 1,953,214,734,956 / ((28조 + 30조) / 2) = 6.735…%
        self.assertIn("자기자본이익률(ROE)은 6.7%입니다.", facts.summary)

    def test_missing_values_become_limitations(self):
        facts, _ = build(["financing_cash_flow"], ["current_ratio"])
        self.assertEqual(facts.summary, "")
        self.assertIn("DB에서 재무활동현금흐름 값을 찾지 못함", facts.limitations)
        self.assertTrue(any("유동비율" in note for note in facts.limitations))

    def test_negative_cash_flow_without_prior(self):
        facts, _ = build(["investing_cash_flow"], [])
        self.assertIn("투자활동현금흐름은 -743,677,268,012원입니다.", facts.summary)

    def test_writer_check_accepts_db_sentence_numbers(self):
        # writer는 DB 문장의 숫자를 그대로 옮기면 검사를 통과해야 한다
        facts, _ = build(["revenue"], ["operating_profit_margin"])
        self.assertTrue({"12035007218975", "18.3"} <= numbers_in(facts.summary))


class TestEvidenceModel(unittest.TestCase):
    def test_db_and_calc_need_no_document_location(self):
        for fmt in ("db", "calc"):
            EvidenceCandidate(source_file="DART DB", chunk_id="c", text="t", source_format=fmt)
        with self.assertRaises(ValueError):
            EvidenceCandidate(source_file="a.xml", chunk_id="c", text="t", source_format="xml")


class TestDropUnsupportedNumbers(unittest.TestCase):
    SOURCE = "49건의 소송사건에 피고(소송가액: 8,947백만원), 13건에 원고(소송가액: 3,803백만원)"

    def test_summed_total_is_dropped(self):
        answer = ("49건에 피고로 계류 중이며 소송가액은 8,947백만원입니다. [S1] "
                  "따라서 총 소송사건 건수는 62건이며, 총 소송가액은 12,750백만원입니다. [S1]")
        kept, dropped = drop_unsupported_numbers(answer, [self.SOURCE])
        self.assertEqual(kept, "49건에 피고로 계류 중이며 소송가액은 8,947백만원입니다. [S1]")
        self.assertEqual(dropped, ["12750", "62"])

    def test_names_and_question_year_are_not_numbers(self):
        kept, dropped = drop_unsupported_numbers("갤럭시 S25 판매가 늘었습니다. [S1]", ["판매 증가"])
        self.assertEqual(dropped, [])
        kept, dropped = drop_unsupported_numbers("2025년 49건입니다. [S1]", ["NAVER 2025년 질문", self.SOURCE])
        self.assertEqual(dropped, [])

    def test_split_keeps_trailing_citation(self):
        self.assertEqual(split_sentences("A는 1.5%입니다. [S1] B입니다. [S2, S3]"),
                         ["A는 1.5%입니다. [S1]", "B입니다. [S2, S3]"])


class TestResearcherMerge(unittest.TestCase):
    def test_template_db_spec(self):
        self.assertIn("total_liabilities", db_spec("stability")[0])
        self.assertIn("debt_ratio", db_spec("stability")[1])
        self.assertEqual(db_spec("risk"), ([], []))

    def test_db_facts_keep_failed_rag_partial(self):
        subtopic = SubTopic(id="stability", title="재무 안정성", questions=["q1"])
        facts, registry = build(["total_liabilities", "total_equity"], ["debt_ratio"])
        failed = failed_finding(subtopic, 1, "부채총계 근거 없음")
        sources = [registry.require(i) for i in facts.evidence_ids]
        from fin_research.research.state import EvidenceRef
        merged = with_db_facts(failed, facts, [EvidenceRef.from_evidence(e) for e in sources])
        self.assertEqual(merged.status, "partial")
        self.assertIn("부채비율은 40.0%(전기 40.0%)", merged.summary)
        self.assertEqual(merged.evidence_ids, ["S1", "S2"])
        self.assertIn("부채총계 근거 없음", merged.limitations)

    def test_db_sentences_come_before_rag_answer(self):
        registry = EvidenceRegistry()
        subtopic = SubTopic(id="profitability", title="수익성", questions=["q1"])
        brief = Brief(company="NAVER", corp_code="00266961", years=[2025], objective="o", subtopics=[subtopic])

        class Store:
            def rows(self, corp_code, year, fs_div):
                return ROWS if fs_div == "CFS" else []

        facts, sources = asyncio.run(collect_db_facts(Store(), brief, subtopic, 1, registry))
        hit = {"evidence_id": "S1", "source_file": "a.xml", "chunk_id": "n1", "text": "광고 매출 증가",
               "metadata": {"source_xpath": "/D[1]"}}
        rag = merge_loop_results(subtopic, 1, [("q1", {
            "findings": {"status": "draft", "answer": "광고 매출이 증가했습니다. [S1]", "evidence_ids": ["S1"]},
            "evidences": [hit]}, None)], registry)
        merged = with_db_facts(rag, facts, sources)
        first, second = merged.summary.split("\n")
        self.assertTrue(first.startswith("연결 포괄손익계산서 기준 매출액은"))
        self.assertEqual(second, f"광고 매출이 증가했습니다. [{rag.evidence_ids[0]}]")
        self.assertEqual(merged.sources[0].quote, registry.require(merged.evidence_ids[0]).text)

    def test_db_failure_keeps_research_going(self):
        subtopic = SubTopic(id="profitability", title="수익성", questions=["q1"])
        brief = Brief(company="X", corp_code="1", years=[2025], objective="o", subtopics=[subtopic])

        class Broken:
            def rows(self, *a):
                raise ConnectionError("timeout")

        facts, sources = asyncio.run(collect_db_facts(Broken(), brief, subtopic, 1, EvidenceRegistry()))
        self.assertEqual((facts.summary, sources), ("", []))
        self.assertIn("DART DB 조회 실패", facts.limitations[0])

    def test_no_store_means_rag_only(self):
        subtopic = SubTopic(id="profitability", title="수익성", questions=["q1"])
        brief = Brief(company="X", corp_code="1", years=[2025], objective="o", subtopics=[subtopic])
        facts, _ = asyncio.run(collect_db_facts(None, brief, subtopic, 1, None))
        self.assertEqual((facts.summary, facts.limitations), ("", []))

    def test_rag_sentence_with_summed_number_is_removed(self):
        hit = {"evidence_id": "S1", "source_file": "a.xml", "chunk_id": "r1", "text": TestDropUnsupportedNumbers.SOURCE,
               "metadata": {"source_xpath": "/D[1]"}}
        answer = "피고 49건입니다. [S1] 총 62건입니다. [S1]"
        finding = merge_loop_results(SubTopic(id="risk", title="리스크", questions=["q"]), 1, [("q", {
            "findings": {"status": "draft", "answer": answer, "evidence_ids": ["S1"]}, "evidences": [hit]}, None)],
            EvidenceRegistry())
        self.assertEqual(finding.summary, "피고 49건입니다. [S1]")
        self.assertTrue(any("62" in note for note in finding.limitations))


if __name__ == "__main__":
    unittest.main()
