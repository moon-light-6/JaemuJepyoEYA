"""verify/accounts.py 테스트: DB 조회 규칙.

행 고르기 규칙은 가짜 행으로 DB 없이 검증한다. 실제 Supabase 조회는 마지막 클래스에서만 하고,
.env에 DATABASE_URL이 없으면 건너뛴다.
"""

import os
import sys
import unittest
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
load_dotenv(PROJECT_ROOT / ".env")  # 실제 DB 테스트의 skipUnless가 .env를 보도록 먼저 읽는다

from fin_research.verify.accounts import (
    AccountStore,
    find_alias,
    load_aliases,
    normalize_name,
    select_value,
)

RCEPT = "20260313001021"


def row(sj_div, account_id, account_nm, current, prior=None, ord_=1):
    return {
        "rcept_no": RCEPT, "fs_div": "CFS", "sj_div": sj_div, "account_id": account_id,
        "account_nm": account_nm, "account_detail": "-", "thstrm_amount": current,
        "frmtrm_amount": prior, "ord": ord_,
    }


# NAVER 2025 연결에서 실제로 나온 값과 구성을 줄여 옮긴 fixture
ROWS = [
    row("BS", "ifrs-full_Assets", "자산 총계", 41_084_496_327_318.0, 38_167_876_036_020.0, 20),
    row("CIS", "ifrs-full_Revenue", "영업수익", 12_035_007_218_975.0, 10_737_700_000_000.0, 1),
    row("CIS", "dart_OperatingIncomeLoss", "영업이익", 2_208_138_388_720.0, 1_979_263_438_827.0, 5),
    row("CIS", "ifrs-full_ProfitLoss", "당기순이익", 1_818_746_310_015.0, 1_900_000_000_000.0, 12),
    row("CF", "ifrs-full_ProfitLoss", "당기순이익", 1_818_746_310_015.0, None, 2),
    # 자본변동표: 지배기업 지분 당기순이익 (골라지면 안 된다)
    row("SCE", "ifrs-full_ProfitLoss", "당기순이익", 1_953_214_734_956.0, None, 3),
    row("CIS", "-표준계정코드 미사용-", "매출원가", 5_000_000_000_000.0, None, 2),
    row("BS", "-표준계정코드 미사용-", "유동부채", 9_000_000_000_000.0, None, 9),
    row("BS", "ifrs-full_CurrentAssets", "유동자산", None, 1.0, 8),
]


class TestAliases(unittest.TestCase):
    def test_config_loads_and_names_are_unique(self):
        aliases = load_aliases()
        self.assertIn("operating_income", aliases)
        self.assertGreaterEqual(len(aliases), 12)

    def test_normalize_name(self):
        self.assertEqual(normalize_name("영업이익 (손실)"), "영업이익손실")
        self.assertEqual(normalize_name("자산 총계"), "자산총계")

    def test_find_alias(self):
        self.assertEqual(find_alias("영업이익(손실)").key, "operating_income")
        self.assertEqual(find_alias("당기연결순이익").key, "net_income")  # SK텔레콤 표기
        self.assertEqual(find_alias("영업활동으로 인한 현금흐름").key, "operating_cash_flow")

    def test_exact_match_only(self):
        self.assertEqual(find_alias("매출").key, "revenue")
        self.assertIsNone(find_alias("매출원가"))
        self.assertIsNone(find_alias("매출총이익"))
        self.assertIsNone(find_alias(""))


class TestSelectValue(unittest.TestCase):
    def pick(self, item, period="current", rows=ROWS):
        return select_value(rows, find_alias(item), period)

    def test_operating_income(self):
        value = self.pick("영업이익")
        self.assertEqual(value.amount, 2_208_138_388_720)
        self.assertEqual((value.account_id, value.sj_div, value.rcept_no), ("dart_OperatingIncomeLoss", "CIS", RCEPT))

    def test_statement_of_changes_in_equity_is_ignored(self):
        # SCE의 1,953,214,734,956이 아니라 CIS의 값
        self.assertEqual(self.pick("당기순이익").amount, 1_818_746_310_015)
        self.assertEqual(self.pick("당기순이익").sj_div, "CIS")

    def test_income_statement_preferred_over_cis(self):
        rows = ROWS + [row("IS", "ifrs-full_ProfitLoss", "당기순이익", 111.0, None, 40)]
        self.assertEqual(self.pick("순이익", rows=rows).sj_div, "IS")

    def test_lower_ord_wins_within_same_statement(self):
        rows = [row("BS", "ifrs-full_Assets", "자산총계", 2.0, None, 9),
                row("BS", "ifrs-full_Assets", "자산총계", 1.0, None, 3)]
        self.assertEqual(self.pick("자산총계", rows=rows).amount, 1.0)

    def test_name_fallback_without_standard_code(self):
        value = self.pick("유동부채")
        self.assertEqual(value.amount, 9_000_000_000_000)
        self.assertEqual(value.account_id, "-표준계정코드 미사용-")

    def test_prior_period(self):
        value = self.pick("영업이익", period="prior")
        self.assertEqual((value.amount, value.period), (1_979_263_438_827, "prior"))
        self.assertEqual(self.pick("영업이익", period="unknown").period, "current")

    def test_missing_amount_or_item(self):
        self.assertIsNone(self.pick("유동자산"))  # 당기값이 비어 있음
        self.assertIsNone(self.pick("현금및현금성자산"))  # 행이 없음

    def test_invalid_period(self):
        with self.assertRaises(ValueError):
            self.pick("영업이익", period="last_year")


class TestAccountStore(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_fetch(corp_code, fiscal_year, fs_div):
            self.calls.append((corp_code, fiscal_year, fs_div))
            return ROWS if fs_div == "CFS" else []
        self.store = AccountStore(fetch=fake_fetch)

    def test_fetches_once_per_company(self):
        self.store.lookup("00266961", 2025, "영업이익")
        self.store.lookup("00266961", 2025, "자산총계")
        self.store.lookup("00266961", 2025, "당기순이익", scope="unknown")  # unknown → CFS
        self.assertEqual(self.calls, [("00266961", 2025, "CFS")])

    def test_unknown_item_does_not_query(self):
        self.assertIsNone(self.store.lookup("00266961", 2025, "직원 수"))
        self.assertEqual(self.calls, [])

    def test_has_company_data(self):
        self.assertTrue(self.store.has_company_data("00266961", 2025))
        self.assertFalse(self.store.has_company_data("00266961", 2025, scope="separate"))

    def test_invalid_scope(self):
        with self.assertRaises(ValueError):
            self.store.lookup("00266961", 2025, "영업이익", scope="group")


@unittest.skipUnless(os.getenv("DATABASE_URL"), "DATABASE_URL 없음")
class TestRealDatabase(unittest.TestCase):
    """Supabase에 적재한 2025 연결 값 (11/20 적재분)."""

    @classmethod
    def setUpClass(cls):
        cls.store = AccountStore()

    def test_naver(self):
        self.assertEqual(self.store.lookup("00266961", 2025, "영업이익").amount, 2_208_138_388_720)
        self.assertEqual(self.store.lookup("00266961", 2025, "당기순이익").amount, 1_818_746_310_015)

    def test_skt_net_income_prefers_income_statement(self):
        value = self.store.lookup("00159023", 2025, "당기순이익")
        self.assertEqual((value.amount, value.sj_div), (375_084_000_000, "IS"))

    def test_kb_has_no_revenue(self):
        # 금융지주: 데이터는 있지만 매출액 항목이 없다 → 노드가 unverifiable로 처리
        self.assertTrue(self.store.has_company_data("00688996", 2025))
        self.assertIsNone(self.store.lookup("00688996", 2025, "매출액"))
        self.assertEqual(self.store.lookup("00688996", 2025, "자산총계").amount, 797_923_033_000_000)


if __name__ == "__main__":
    unittest.main()
