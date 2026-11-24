"""verify_claim 노드: 주장 하나를 DB 기준값과 비교해 Verdict를 만든다.

claim_extract가 주장마다 Send로 병렬 실행한다. 1차 버전은 LLM을 쓰지 않는다.
  항목 이름 → accounts.find_alias / 비율 공식 → AccountStore.lookup(DB) → numbers.compare

판정 순서 (처음 걸리는 것):
  ① 숫자 없음                         → unverifiable
  ② 지원하지 않는 항목                → unverifiable
  ③ 보고서 연도·전년도가 아닌 연도     → unverifiable
  ④ 그 기업·연결별도 데이터가 DB에 없음 → not_found   (우리 데이터 문제)
  ⑤ 데이터는 있는데 그 항목 값이 없음   → unverifiable (금융지주의 매출액 등, 주장의 문제)
  ⑥ 단위 해석 불가                     → unverifiable
  ⑦ 비교 결과                          → match / mismatch
"""
from __future__ import annotations

import asyncio
import re
from functools import lru_cache
from typing import NamedTuple

from langchain_core.runnables import RunnableConfig

from fin_research.verify.accounts import (
    SCOPE_TO_FS_DIV,
    AccountStore,
    AccountValue,
    find_alias,
    normalize_name,
)
from fin_research.verify.numbers import PERCENT, compare, normalize_unit, to_won
from fin_research.verify.state import Claim, ClaimVerifyInput, Verdict, VerifyEvidence

DB_SOURCE = "fin.dart_financial_accounts"

# 비율 = 분자 / 분모 × 100. 이름은 normalize_name 기준.
RATIOS: dict[str, tuple[str, str]] = {
    "부채비율": ("부채총계", "자본총계"),
    "유동비율": ("유동자산", "유동부채"),
    "영업이익률": ("영업이익", "매출액"),
    "순이익률": ("당기순이익", "매출액"),
    "당기순이익률": ("당기순이익", "매출액"),
    "자기자본비율": ("자본총계", "자산총계"),
}

try:  # DB 장애는 주장 하나의 not_found로 끝내고, 코드 버그(ValueError 등)는 숨기지 않는다
    import psycopg2

    DB_ERRORS: tuple[type[BaseException], ...] = (psycopg2.Error, RuntimeError, TimeoutError, OSError)
except ImportError:  # pragma: no cover
    DB_ERRORS = (RuntimeError, TimeoutError, OSError)


# ─────────────────────────── 도우미 (순수 함수) ───────────────────────────

@lru_cache(maxsize=1)
def _default_store() -> AccountStore:
    return AccountStore()


def get_account_store(config: RunnableConfig | None) -> AccountStore:
    """config["configurable"]["account_store"]. 병렬 노드가 같은 캐시를 쓰도록 그래프가 넣어 준다."""
    store = ((config or {}).get("configurable") or {}).get("account_store")
    return store or _default_store()


def format_won(won: float | None) -> str:
    """2208138388720 → '2.21조원', 58400000000 → '584억원', 50000000 → '5,000만원', 1234 → '1,234원'."""
    if won is None:
        return "-"
    if abs(won) >= 10**12:
        return f"{won / 10**12:,.2f}조원"
    if abs(won) >= 10**8:
        return f"{won / 10**8:,.0f}억원"
    if abs(won) >= 10**4:
        return f"{won / 10**4:,.0f}만원"
    return f"{won:,.0f}원"


def format_value(value: float | None, is_ratio: bool) -> str:
    if value is None:
        return "-"
    return f"{value:,.1f}%" if is_ratio else format_won(value)


def resolve_period(claim: Claim, fiscal_year: int) -> tuple[str | None, list[str]]:
    """(current/prior 또는 None=범위 밖, 가정 메모). year가 있으면 period보다 year를 믿는다."""
    if claim.year is not None:
        if claim.year == fiscal_year:
            return "current", []
        if claim.year == fiscal_year - 1:
            return "prior", []
        return None, []
    if claim.period == "prior":
        return "prior", [f"{fiscal_year - 1}년(전기)으로 가정"]
    return "current", [f"{fiscal_year}년으로 가정"]


_LOSS_IN_PARENS = re.compile(r"\(\s*손실\s*\)")


def signed_value(claim: Claim) -> float | None:
    """'영업손실 1,234억'(value=1234) → -1234. '영업이익(손실)'은 계정명이라 그대로."""
    value = claim.value
    if value is None:
        return None
    is_loss = "손실" in claim.item and not _LOSS_IN_PARENS.search(claim.item)
    return -value if is_loss and value > 0 else value


class Target(NamedTuple):
    is_ratio: bool
    label: str  # 화면에 보여 줄 기준 항목
    parts: tuple[str, ...]  # 조회할 항목 이름 (금액은 1개, 비율은 분자·분모)


def resolve_target(item: str) -> Target | None:
    key = normalize_name(item)
    if key in RATIOS:
        numerator, denominator = RATIOS[key]
        return Target(True, f"{numerator}/{denominator}", (numerator, denominator))
    spec = find_alias(item)
    if spec is None:
        return None
    return Target(False, spec.label, (item,))


def _verdict(claim: Claim, label: str, explanation: str, notes: list[str], **fields) -> Verdict:
    evidence = [VerifyEvidence(id=f"{claim.id}.pdf", kind="pdf", pdf_page=claim.page)] if claim.page else []
    if fields.get("db_value_won") is not None:
        evidence.append(VerifyEvidence(id=f"{claim.id}.db", kind="db", source_file=DB_SOURCE))
    text = " · ".join([explanation, *notes])
    return Verdict(claim_id=claim.id, label=label, explanation=text, evidence=evidence, **fields)


# ─────────────────────────── 판정 (DB 조회는 store로) ───────────────────────────

def judge(claim: Claim, store: AccountStore, corp_code: str, fiscal_year: int,
          rcept_no: str | None = None) -> Verdict:
    """주장 하나 → Verdict. DB 장애(DB_ERRORS)는 호출하는 쪽이 처리한다."""
    notes: list[str] = []
    value = signed_value(claim)
    # ① 숫자
    if value is None:
        return _verdict(claim, "unverifiable", "주장에서 숫자를 읽지 못했습니다", notes)
    # ② 항목
    target = resolve_target(claim.item)
    if target is None:
        return _verdict(claim, "unverifiable", f"'{claim.item}'은(는) 검증 지원 항목이 아닙니다", notes)
    # ③ 연도 → 당기/전기
    period, assumed = resolve_period(claim, fiscal_year)
    if period is None:
        return _verdict(claim, "unverifiable",
                        f"{claim.year}년 자료는 {fiscal_year}년 사업보고서에 없습니다", notes)
    notes += assumed
    scope = claim.scope
    if scope == "unknown":
        notes.append("연결 기준으로 가정")
    # ④ 기업 데이터
    if not store.has_company_data(corp_code, fiscal_year, scope):
        basis = "별도" if SCOPE_TO_FS_DIV[scope] == "OFS" else "연결"
        return _verdict(claim, "not_found", f"{basis} 기준 재무 데이터가 적재되어 있지 않습니다", notes)
    # ⑤ 항목 값
    values: list[AccountValue] = []
    for part in target.parts:
        found = store.lookup(corp_code, fiscal_year, part, scope, period)
        if found is None:
            return _verdict(claim, "unverifiable", f"이 기업 재무제표에 '{part}' 항목이 없습니다", notes)
        values.append(found)
    if target.is_ratio:
        numerator, denominator = (v.amount for v in values)
        if denominator == 0:
            return _verdict(claim, "unverifiable", f"{target.label}의 분모가 0입니다", notes)
        db_value = numerator / denominator * 100
        unit = claim.unit if normalize_unit(claim.unit) == PERCENT else "%"
        account_id, account_nm = f"ratio:{normalize_name(claim.item)}", target.label
    else:
        db_value, unit = values[0].amount, claim.unit
        account_id, account_nm = values[0].account_id, values[0].account_nm
    db_rcept = values[0].rcept_no
    if rcept_no and db_rcept != rcept_no:
        notes.append(f"DB 공시({db_rcept})가 설정 공시({rcept_no})와 다릅니다")
    common = {"db_value_won": db_value, "account_id": account_id, "account_nm": account_nm, "rcept_no": db_rcept}
    # ⑥ 비교 (원문 문자열 대신 값으로: 정밀도는 claim_extract가 원문 단위로 맞춰 둔다)
    result = compare(value, unit, db_value)
    if result is None:
        unit_text = f"단위 '{claim.unit}'을(를) 해석할 수 없습니다" if claim.unit else "주장에 단위가 없습니다"
        return _verdict(claim, "unverifiable", unit_text, notes,
                        claim_value_won=to_won(value, unit), **common)
    # ⑦ 일치 / 불일치
    db_text = format_value(db_value, target.is_ratio)
    claim_text = format_value(result.claim_won, target.is_ratio)
    if result.label == "match":
        # 비율 허용 오차는 0.05%p처럼 작아서 소수 첫째 자리로 반올림하면 틀린 값이 보인다
        tolerance = f"{result.tolerance_won:g}%p" if target.is_ratio else format_won(result.tolerance_won)
        explanation = f"DB {db_text}과 일치 (주장 {claim_text}, 허용 ±{tolerance})"
    else:
        if target.is_ratio:
            diff = f"{result.claim_won - db_value:+.1f}%p"
        elif result.diff_ratio is not None:
            diff = f"{result.diff_ratio:+.1%}"
        else:
            diff = "DB 값 0"
        explanation = f"DB {db_text}과 다름 (주장 {claim_text}, {diff})"
    return _verdict(claim, result.label, explanation, notes, claim_value_won=result.claim_won,
                    diff_ratio=result.diff_ratio, **common)


# ─────────────────────────── 노드 ───────────────────────────

async def verify_claim_node(state: ClaimVerifyInput, config: RunnableConfig) -> dict:
    """병렬 노드라 주장 하나의 실패가 전체를 멈추지 않게 한다. 다음은 항상 aggregate (엣지)."""
    claim = state["claim"]
    corp_code, fiscal_year = state.get("corp_code"), state.get("fiscal_year")
    if not corp_code or not fiscal_year:
        verdict = _verdict(claim, "not_found", "검증할 기업·사업연도가 정해지지 않았습니다", [])
        return {"verdicts": [verdict]}
    store = get_account_store(config)
    try:
        # 첫 조회는 DB에 접속하는 동기 함수라 스레드로 보낸다 (다른 병렬 노드가 멈추지 않게)
        verdict = await asyncio.to_thread(judge, claim, store, corp_code, int(fiscal_year), state.get("rcept_no"))
    except DB_ERRORS as e:
        verdict = _verdict(claim, "not_found", "DB 조회에 실패했습니다", [])
        return {"verdicts": [verdict], "errors": [f"verify_claim {claim.id}: {type(e).__name__}: {e}"]}
    return {"verdicts": [verdict]}
