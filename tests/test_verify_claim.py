"""verify_claim 노드: 판정 순서 ①~⑦과 노드 동작(캐시 공유·DB 장애)을 검증합니다.

DB 대신 가짜 fetch를 넣은 AccountStore를 config로 넘긴다. 값은 NAVER 2025 연결 실제 값을 줄여 옮겼다.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research.verify.accounts import AccountStore
from fin_research.verify.nodes.verify_claim import (
    format_won,
    judge,
    resolve_period,
    signed_value,
    verify_claim_node,
)
from fin_research.verify.state import Claim

CORP, YEAR, RCEPT = "00266961", 2025, "20260313001021"


def row(sj_div, account_id, account_nm, current, prior=None, ord_=1):
    return {"rcept_no": RCEPT, "fs_div": "CFS", "sj_div": sj_div, "account_id": account_id,
            "account_nm": account_nm, "account_detail": "-", "thstrm_amount": current,
            "frmtrm_amount": prior, "ord": ord_}


ROWS = [
    row("CIS", "ifrs-full_Revenue", "영업수익", 12_035_007_218_975.0, 10_737_700_000_000.0, 1),
    row("CIS", "dart_OperatingIncomeLoss", "영업이익", 2_208_138_388_720.0, 1_979_263_438_827.0, 5),
    row("CIS", "ifrs-full_ProfitLoss", "당기순이익", 1_818_746_310_015.0, None, 12),
    row("BS", "ifrs-full_Assets", "자산총계", 41_084_496_327_318.0, None, 20),
    row("BS", "ifrs-full_Liabilities", "부채총계", 12_131_482_961_723.0, None, 30),
    row("BS", "ifrs-full_Equity", "자본총계", 28_953_013_365_595.0, None, 40),
    row("CF", "-표준계정코드 미사용-", "이자의 지급", -123_400_000_000.0, None, 9),
]


def claim(item, value, unit="조원", year=2025, **kw):
    return Claim(id=kw.pop("id", "C1"), text=f"{item} {value}{unit or ''}", item=item, value=value,
                 unit=unit, year=year, page=kw.pop("page", 2), **kw)


class FakeFetch:
    def __init__(self, rows=ROWS, error=None):
        self.rows, self.error, self.calls = rows, error, []

    def __call__(self, corp_code, fiscal_year, fs_div):
        self.calls.append((corp_code, fiscal_year, fs_div))
        if self.error:
            raise self.error
        return self.rows if fs_div == "CFS" else []


class TestHelpers(unittest.TestCase):
    def test_format_won(self):
        self.assertEqual(format_won(2_208_138_388_720), "2.21조원")
        self.assertEqual(format_won(58_400_000_000), "584억원")
        self.assertEqual(format_won(50_000_000), "5,000만원")  # '41조 845억'의 허용 오차 ±0.5억
        self.assertEqual(format_won(-1234), "-1,234원")

    def test_resolve_period(self):
        self.assertEqual(resolve_period(claim("영업이익", 2.2, year=2025), YEAR), ("current", []))
        self.assertEqual(resolve_period(claim("영업이익", 2.2, year=2024, period="current"), YEAR)[0], "prior")
        self.assertIsNone(resolve_period(claim("영업이익", 2.2, year=2023), YEAR)[0])
        self.assertEqual(resolve_period(claim("영업이익", 2.2, year=None), YEAR), ("current", ["2025년으로 가정"]))
        self.assertEqual(resolve_period(claim("영업이익", 2.2, year=None, period="prior"), YEAR)[0], "prior")

    def test_signed_value(self):
        self.assertEqual(signed_value(claim("영업손실", 1234, "억원")), -1234)
        self.assertEqual(signed_value(claim("영업이익(손실)", 1234, "억원")), 1234)
        self.assertEqual(signed_value(claim("영업손실", -1234, "억원")), -1234)


class TestJudge(unittest.TestCase):
    """판정 순서 ①~⑦을 표처럼 하나씩."""

    def setUp(self):
        self.fetch = FakeFetch()
        self.store = AccountStore(fetch=self.fetch)

    def judge(self, c, rcept_no=RCEPT):
        return judge(c, self.store, CORP, YEAR, rcept_no)

    def test_match_rounded_amount(self):
        v = self.judge(claim("영업이익", 2.2, scope="consolidated"))
        self.assertEqual(v.label, "match")
        self.assertEqual(v.db_value_won, 2_208_138_388_720)
        self.assertEqual((v.account_id, v.rcept_no), ("dart_OperatingIncomeLoss", RCEPT))
        self.assertIn("DB 2.21조원과 일치", v.explanation)
        self.assertEqual([e.kind for e in v.evidence], ["pdf", "db"])

    def test_mismatch_with_diff(self):
        v = self.judge(claim("당기순이익", 2.1))
        self.assertEqual(v.label, "mismatch")
        self.assertAlmostEqual(v.diff_ratio, 0.1546, places=3)
        self.assertIn("+15.5%", v.explanation)
        self.assertIn("연결 기준으로 가정", v.explanation)

    def test_prior_year_uses_prior_amount(self):
        v = self.judge(claim("영업이익", 1.98, year=2024))
        self.assertEqual((v.label, v.db_value_won), ("match", 1_979_263_438_827))

    def test_missing_year_assumes_report_year(self):
        v = self.judge(claim("영업이익", 2.2, year=None))
        self.assertEqual(v.label, "match")
        self.assertIn("2025년으로 가정", v.explanation)

    def test_mixed_amount_in_eok(self):
        # claim_extract가 '41조 845억'을 410845억원으로 넘긴다 → ±0.5억 → 실제 41조 844.96억과 일치
        self.assertEqual(self.judge(claim("자산총계", 410845, "억원")).label, "match")

    def test_loss_claim_matches_negative_db(self):
        rows = ROWS + [row("IS", "dart_OperatingIncomeLoss", "영업이익(손실)", -123_400_000_000.0, None, 1)]
        store = AccountStore(fetch=FakeFetch(rows))
        self.assertEqual(judge(claim("영업손실", 1234, "억원"), store, CORP, YEAR).label, "match")

    def test_ratio_computed_from_db(self):
        v = self.judge(claim("부채비율", 41.9, "%"))  # 12.13 / 28.95 = 41.90%
        self.assertEqual(v.label, "match")
        self.assertEqual(v.account_nm, "부채총계/자본총계")
        self.assertIn("±0.05%p", v.explanation)
        wrong = self.judge(claim("부채 비율", 45, None))  # 단위가 없어도 비율로 본다
        self.assertEqual(wrong.label, "mismatch")
        self.assertIn("+3.1%p", wrong.explanation)

    def test_no_number(self):  # ①
        self.assertEqual(self.judge(claim("영업이익", None)).label, "unverifiable")

    def test_unsupported_item_does_not_query(self):  # ②
        v = self.judge(claim("직원 수", 4000, "명"))
        self.assertEqual(v.label, "unverifiable")
        self.assertEqual(self.fetch.calls, [])

    def test_out_of_range_year(self):  # ③
        v = self.judge(claim("영업이익", 1.3, year=2023))
        self.assertEqual(v.label, "unverifiable")
        self.assertIn("2023년 자료", v.explanation)

    def test_separate_without_data_is_not_found(self):  # ④
        v = self.judge(claim("영업이익", 1.5, scope="separate"))
        self.assertEqual(v.label, "not_found")
        self.assertIn("별도 기준", v.explanation)

    def test_item_missing_for_company_is_unverifiable(self):  # ⑤ (금융지주의 매출액)
        store = AccountStore(fetch=FakeFetch([r for r in ROWS if r["account_id"] != "ifrs-full_Revenue"]))
        v = judge(claim("매출액", 12, "조원"), store, CORP, YEAR)
        self.assertEqual(v.label, "unverifiable")
        self.assertIn("'매출액' 항목이 없습니다", v.explanation)

    def test_unknown_unit(self):  # ⑥
        v = self.judge(claim("영업이익", 2.2, "달러"))
        self.assertEqual(v.label, "unverifiable")
        self.assertEqual(v.db_value_won, 2_208_138_388_720)

    def test_rcept_mismatch_is_noted(self):
        v = self.judge(claim("영업이익", 2.2), rcept_no="20260101000001")
        self.assertIn("설정 공시(20260101000001)와 다릅니다", v.explanation)


class TestNode(unittest.TestCase):
    def run_node(self, c, fetch, **state):
        config = {"configurable": {"account_store": AccountStore(fetch=fetch)}}
        payload = {"claim": c, "corp_code": CORP, "fiscal_year": YEAR, "rcept_no": RCEPT, **state}
        return asyncio.run(verify_claim_node(payload, config))

    def test_returns_one_verdict(self):
        out = self.run_node(claim("영업이익", 2.2), FakeFetch())
        self.assertEqual([v.label for v in out["verdicts"]], ["match"])
        self.assertNotIn("errors", out)

    def test_db_failure_becomes_not_found(self):
        out = self.run_node(claim("영업이익", 2.2), FakeFetch(error=psycopg2.OperationalError("timeout")))
        self.assertEqual(out["verdicts"][0].label, "not_found")
        self.assertIn("OperationalError", out["errors"][0])

    def test_code_bug_is_not_hidden(self):
        with self.assertRaises(KeyError):
            self.run_node(claim("영업이익", 2.2), FakeFetch(error=KeyError("버그")))

    def test_missing_company(self):
        out = self.run_node(claim("영업이익", 2.2), FakeFetch(), corp_code=None)
        self.assertEqual(out["verdicts"][0].label, "not_found")

    def test_parallel_claims_share_one_fetch(self):
        fetch = FakeFetch()
        store = AccountStore(fetch=fetch)
        config = {"configurable": {"account_store": store}}
        claims = [claim("영업이익", 2.2, id="C1"), claim("자산총계", 41.1, id="C2"), claim("부채비율", 41.9, "%", id="C3")]

        async def run_all():
            return await asyncio.gather(*(verify_claim_node(
                {"claim": c, "corp_code": CORP, "fiscal_year": YEAR, "rcept_no": RCEPT}, config) for c in claims))
        results = asyncio.run(run_all())
        self.assertEqual([r["verdicts"][0].label for r in results], ["match", "match", "match"])
        self.assertEqual(fetch.calls, [(CORP, YEAR, "CFS")])


if __name__ == "__main__":
    unittest.main()
