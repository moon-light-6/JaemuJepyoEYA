"""claim_extract 노드: 페이지 묶기·후처리 규칙과 노드 동작(병렬 호출·오류·예산·라우팅)을 검증합니다.

LLM은 FakeMessagesListChatModel로 바꿔 끼우고, PDF 대신 API 형식의 추출 JSON을 임시 파일로 쓴다.
"""

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
from fin_research.verify.nodes import claim_extract
from fin_research.verify.nodes.claim_extract import (
    batch_pages,
    claim_extract_node,
    dedupe,
    fill_value,
    ground_claim,
    korean_amounts,
    load_pages,
    postprocess,
    resolve_company,
)
from fin_research.verify.state import ClaimDraft

PAGES = [
    (1, "NAVER 2025년 재무 분석 보고서\n요약"),
    (2, "2025년 연결 영업이익은 2.2조원으로\n전년 대비 증가했다. 매출 12조, 영업이익 2.2조를 기록했다."),
    (3, "자산총계는 41조 845억원이다. 부채비율은 41.9%이다."),
]


def draft(text, item="영업이익", value=None, unit=None, page=None, **kw):
    return ClaimDraft(text=text, item=item, value=value, unit=unit, page=page, **kw)


class TestPages(unittest.TestCase):
    def test_load_pages_from_api_json_skips_blank(self):
        data = {"pages": [
            {"page_number": 1, "text": "본문", "is_blank": False},
            {"page_number": 2, "text": "   ", "is_blank": True},
            {"page_number": 3, "text": "", "is_blank": False},
        ]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "job_pages.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_pages(path), [(1, "본문")])

    def test_batch_pages_marks_pages_and_respects_size(self):
        batches = batch_pages([(1, "a" * 10), (2, "b" * 10), (3, "c" * 50)], max_chars=40)
        self.assertEqual(len(batches), 2)
        self.assertTrue(batches[0].startswith("[p.1]\n"))
        self.assertIn("[p.2]", batches[0])
        self.assertTrue(batches[1].startswith("[p.3]"))  # 긴 쪽은 혼자, 자르지 않음
        self.assertIn("c" * 50, batches[1])


class TestPostprocess(unittest.TestCase):
    by_page = dict(PAGES)

    def test_ground_keeps_text_across_line_breaks(self):
        d = draft("2025년 연결 영업이익은 2.2조원으로 전년 대비 증가했다.", page=2)
        self.assertEqual(ground_claim(d, self.by_page).page, 2)

    def test_ground_fixes_wrong_page(self):
        d = draft("자산총계는 41조 845억원이다.", item="자산총계", page=1)
        self.assertEqual(ground_claim(d, self.by_page).page, 3)

    def test_ground_drops_invented_text(self):
        d = draft("자본총계는 100,000,000원이다 (예시로 기재).", item="자본총계", page=3)
        self.assertIsNone(ground_claim(d, self.by_page))

    def test_korean_amounts_keep_smallest_unit(self):
        self.assertEqual(korean_amounts("자산총계는 41조 845억원이다."), [(410845.0, "억원")])
        self.assertEqual(korean_amounts("영업이익 2.2조원"), [(2.2, "조원")])
        self.assertEqual(korean_amounts("순손실 △1,345억원"), [(-1345.0, "억원")])
        self.assertEqual(len(korean_amounts("매출 12조, 영업이익 2.2조")), 2)

    def test_fill_value_reads_mixed_amount(self):
        filled = fill_value(draft("자산총계는 41조 845억원이다.", item="자산총계", unit="원"))
        self.assertEqual((filled.value, filled.unit), (410845.0, "억원"))

    def test_fill_value_restores_original_unit(self):
        # LLM이 원으로 환산해 버린 경우: 값은 같아도 허용 오차를 위해 원문 단위로
        filled = fill_value(draft("영업이익 2.2조원", value=2.2e12, unit="원"))
        self.assertEqual((filled.value, filled.unit), (2.2, "조원"))

    def test_fill_value_fixes_wrong_llm_value(self):
        filled = fill_value(draft("영업이익 2.2조원", value=22, unit="조원"))
        self.assertEqual(filled.value, 2.2)

    def test_fill_value_uses_amount_after_item_name(self):
        text = "자산총계는 41조 845억원, 부채총계는 12.1조원이다."
        self.assertEqual(fill_value(draft(text, item="자산총계", unit="원")).value, 410845.0)
        self.assertEqual(fill_value(draft(text, item="부채총계")).value, 12.1)
        # 항목 이름이 문장에 없으면 정하지 않는다
        self.assertIsNone(fill_value(draft(text, item="총자산")).value)

    def test_split_mixed_amount_is_rejoined_and_deduped(self):
        # 실제 LLM이 '41조 845억원'을 41조원·845억원 두 주장으로 쪼갠 사례
        text = "자산총계는 41조 845억원, 부채총계는 12.1조원이다."
        pieces = [draft(text, item="자산총계", value=41, unit="조원", page=3),
                  draft(text, item="자산총계", value=845, unit="억원", page=3)]
        self.assertEqual([(f.value, f.unit) for f in map(fill_value, pieces)], [(410845.0, "억원")] * 2)
        kept, _ = postprocess(pieces, [(3, text)], max_claims=10)
        self.assertEqual(len(kept), 1)

    def test_fill_value_keeps_prior_period_value(self):
        # 같은 문장의 전기 주장(1.98조)을 당기 금액으로 덮어쓰지 않는다
        prior = draft("영업이익은 2.2조원으로 전년 1.98조원 대비 증가했다.", value=1.98, unit="조원", period="prior")
        self.assertIs(fill_value(prior), prior)

    def test_fill_value_leaves_ambiguous_or_ratio(self):
        two = draft("매출 12조, 영업이익 2.2조를 기록했다.", value=2.2, unit="조원")
        self.assertIs(fill_value(two), two)
        ratio = draft("자산 41조원, 부채비율 41.9", item="부채비율", value=41.9, unit=None)
        self.assertIs(fill_value(ratio), ratio)
        pct = draft("부채비율은 41.9%이다.", item="부채비율", value=41.9, unit="%")
        self.assertIs(fill_value(pct), pct)

    def test_dedupe_same_amount_in_different_units(self):
        a = draft("영업이익 2.2조원", value=2.2, unit="조원", year=2025)
        b = draft("영업이익 22,000억원", value=22000, unit="억원", year=2025)
        c = draft("영업이익 2.2조원", value=2.2, unit="조원", year=2024)
        self.assertEqual(dedupe([a, b, c]), [a, c])

    def test_postprocess_notes_and_limit(self):
        drafts = [
            draft("2025년 연결 영업이익은 2.2조원으로", value=2.2, unit="조원", page=2),
            draft("자산총계는 41조 845억원이다.", item="자산총계", page=3),
            draft("부채비율은 41.9%이다.", item="부채비율", value=41.9, unit="%", page=3),
            draft("없는 문장", page=1),
        ]
        kept, notes = postprocess(drafts, PAGES, max_claims=2)
        self.assertEqual([k.item for k in kept], ["영업이익", "자산총계"])
        self.assertIn("찾지 못한 주장 1개", notes[0])
        self.assertIn("3개 중 앞의 2개", notes[1])


class TestResolveCompany(unittest.TestCase):
    def test_known_company(self):
        company, error = resolve_company("NAVER")
        self.assertIsNone(error)
        self.assertEqual(company["corp_code"], "00266961")

    def test_missing_or_unknown(self):
        self.assertIn("지정되지 않았습니다", resolve_company(None)[1])
        self.assertIn("하나로 정할 수 없습니다", resolve_company("애플")[1])


class TestClaimExtractNode(unittest.TestCase):
    USAGE = {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70}

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pages_path = Path(self.directory.name) / "job_pages.json"
        self.write_pages(PAGES)

    def write_pages(self, pages):
        data = {"pages": [{"page_number": n, "text": t, "is_blank": not t.strip()} for n, t in pages]}
        self.pages_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def factory(self, *responses):
        messages = [AIMessage(r, usage_metadata=self.USAGE) for r in responses]

        def make(role, budget=None, **_):
            self.assertEqual(role, "claim_extractor")
            callbacks = [BudgetCallbackHandler(budget, role)] if budget else None
            return FakeMessagesListChatModel(responses=messages, callbacks=callbacks)
        return make

    def run_node(self, factory, company="NAVER", settings=None, config=None):
        patches = [patch.object(claim_extract, "get_llm_for_role", side_effect=factory)]
        if settings:
            patches.append(patch.object(claim_extract, "verify_settings",
                                        return_value={**claim_extract.DEFAULTS, **settings}))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        state = {"pdf_path": str(self.pages_path), "company": company}
        return asyncio.run(claim_extract_node(state, config or {}))

    @staticmethod
    def extraction(*claims):
        return json.dumps({"claims": list(claims)}, ensure_ascii=False)

    def test_extracts_claims_and_sends_each_to_verify(self):
        response = self.extraction(
            {"text": "2025년 연결 영업이익은 2.2조원으로", "item": "영업이익", "value": 2.2, "unit": "조원",
             "year": 2025, "scope": "consolidated", "page": 2},
            {"text": "자산총계는 41조 845억원이다.", "item": "자산총계", "unit": "원", "page": 3},
        )
        out = self.run_node(self.factory(response))
        claims = out.update["claims"]
        self.assertEqual([c.id for c in claims], ["C1", "C2"])
        self.assertEqual((claims[1].value, claims[1].unit), (410845.0, "억원"))
        self.assertEqual([s.node for s in out.goto], ["verify_claim", "verify_claim"])
        self.assertEqual(out.goto[0].arg["corp_code"], "00266961")
        self.assertEqual(out.goto[0].arg["claim"].id, "C1")
        self.assertEqual(out.update["fiscal_year"], 2025)
        self.assertEqual(out.update["errors"], [])

    def test_one_bad_batch_keeps_others(self):
        # 쪽마다 한 묶음 → LLM 3번. 두 번째 묶음만 JSON 실패(재시도까지 3회 실패)
        good = self.extraction({"text": "NAVER 2025년 재무 분석 보고서", "item": "x", "page": 1})
        bad = "JSON이 아닙니다"
        last = self.extraction({"text": "부채비율은 41.9%이다.", "item": "부채비율", "value": 41.9, "unit": "%", "page": 3})
        responses = [good, bad, bad, bad, last]

        # 동시 실행이라 응답 순서를 정하려고 묶음 하나씩 차례로 보낸다
        out = self.run_node(self.factory(*responses), settings={"batch_max_chars": 10, "max_concurrent_extract": 1})
        self.assertEqual([c.item for c in out.update["claims"]], ["x", "부채비율"])
        self.assertEqual(len(out.update["errors"]), 1)
        self.assertIn("2번째 묶음", out.update["errors"][0])

    def test_no_claims_goes_to_aggregate(self):
        out = self.run_node(self.factory(self.extraction()))
        self.assertEqual(out.goto, "aggregate")
        self.assertEqual(out.update["claims"], [])

    def test_unknown_company_stops_without_llm(self):
        def must_not_call(*_, **__):
            raise AssertionError("기업이 없으면 LLM을 만들면 안 됩니다")
        out = self.run_node(must_not_call, company="애플")
        self.assertEqual(out.goto, "aggregate")
        self.assertIn("애플", out.update["errors"][0])

    def test_unreadable_pdf_stops(self):
        self.pages_path.write_text("{깨진 JSON", encoding="utf-8")
        out = self.run_node(self.factory())
        self.assertEqual(out.goto, "aggregate")
        self.assertIn("PDF를 읽지 못했습니다", out.update["errors"][0])

    def test_budget_is_recorded_and_exceeded_is_reported(self):
        budget = Budget(max_llm_calls=1)
        response = self.extraction({"text": "NAVER 2025년 재무 분석 보고서", "item": "x", "page": 1})
        out = self.run_node(
            self.factory(response, response), settings={"batch_max_chars": 10, "max_concurrent_extract": 1},
            config={"configurable": {"budget": budget}},
        )
        self.assertEqual(budget.summary()["by_role"]["claim_extractor"]["calls"], 1)
        self.assertEqual(len(out.update["claims"]), 1)  # 첫 묶음 결과는 살린다
        self.assertTrue(any("[예산 초과]" in e for e in out.update["errors"]))


if __name__ == "__main__":
    unittest.main()
