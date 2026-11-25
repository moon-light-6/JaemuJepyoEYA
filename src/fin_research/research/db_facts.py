"""Hybrid RAG의 DB 쪽: 재무제표 숫자는 DART DB에서 코드가 가져오고, 비율은 코드가 계산한다.

문서 검색(RAG)은 표가 여러 청크로 쪼개지거나(재무상태표 합계), 같은 이름이 다른 표에 있으면
(자본변동표의 당기순이익) 틀린 숫자를 고를 수 있다. 숫자는 DB, 설명은 문서로 나눈다.

- 계정값: verify/accounts.py의 select_value (본표만 · 표준 계정코드 우선 · 자본변동표 제외)
- 비율: tools/ratios.py (Decimal 계산, 반올림은 표시할 때 소수 첫째 자리)
- 출처: 재무제표별 DB 근거 1개 + 비율마다 계산 근거 1개를 장부에 등록해 [S#]로 인용
- 문장: 코드가 쓴다 (LLM 없음). writer는 이 문장의 숫자를 그대로 옮긴다.

DB 조회(store.rows)는 researcher가 하고, 여기는 행(rows)을 받아 문장·근거를 만드는 순수 함수만 둔다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from fin_research.tools import ratios
from fin_research.verify.accounts import AccountValue, Row, load_aliases, select_value

STATEMENT_TITLE = {"BS": "재무상태표", "IS": "손익계산서", "CIS": "포괄손익계산서", "CF": "현금흐름표"}
FS_LABEL = {"CFS": "연결", "OFS": "별도"}
DB_SOURCE_FORMAT = "db"
CALC_SOURCE_FORMAT = "calc"

# 비율 키 → (이름, 계산식 설명, 분자 계정, 분모 계정, 함수)
SIMPLE_RATIOS: dict[str, tuple[str, str, str, str, Callable]] = {
    "operating_profit_margin": ("영업이익률", "영업이익 / 매출액 × 100", "operating_income", "revenue",
                                ratios.operating_profit_margin),
    "debt_ratio": ("부채비율", "부채총계 / 자본총계 × 100", "total_liabilities", "total_equity",
                   ratios.debt_ratio),
    "current_ratio": ("유동비율", "유동자산 / 유동부채 × 100", "current_assets", "current_liabilities",
                      ratios.current_ratio),
}
ROE_NAME = "자기자본이익률(ROE)"
ROE_FORMULA = "당기순이익 / ((기초 자본총계 + 기말 자본총계) / 2) × 100"


@dataclass
class DbFacts:
    """한 하위 주제의 DB 결과. summary가 비어 있으면 DB로 확인한 숫자가 없다."""

    summary: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


# ─────────────────────────── 표시 형식 ───────────────────────────

def won(amount: float) -> str:
    """원 단위 정수, 쉼표. -743677268012.0 → '-743,677,268,012원'."""
    return f"{int(round(amount)):,}원"


def percent(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def topic(word: str) -> str:
    """주제 조사: '매출액은', '부채총계는'. 마지막 한글 글자의 받침으로 고른다."""
    for ch in reversed(word):
        if "가" <= ch <= "힣":
            return f"{word}{'은' if (ord(ch) - 0xAC00) % 28 else '는'}"
    return f"{word}은"


def _decimal(amount: float) -> Decimal:
    # ratios.py는 float을 받지 않는다 (이진 부동소수점 오차 방지). DB 금액은 원 단위 정수다.
    return Decimal(int(round(amount)))


# ─────────────────────────── 계정값 ───────────────────────────

def lookup(rows: list[Row], key: str, period: str = "current") -> AccountValue | None:
    return select_value(rows, load_aliases()[key], period)


def _amount_text(current: AccountValue, prior: AccountValue | None) -> str:
    text = won(current.amount)
    return f"{text}(전기 {won(prior.amount)})" if prior else text


def _ratio(fn: Callable, *values: AccountValue | None) -> Decimal | None:
    if any(v is None for v in values):
        return None
    try:
        return fn(*(_decimal(v.amount) for v in values))
    except ValueError:  # UndefinedRatioError 포함 (분모 0 이하 등)
        return None


# ─────────────────────────── 문장·근거 만들기 ───────────────────────────

def build_db_facts(
    rows: list[Row],
    *,
    items: list[str],
    ratio_keys: list[str],
    company: str,
    corp_code: str,
    year: int,
    fs_div: str,
    subtopic_id: str,
    register: Callable[[dict], str],
) -> DbFacts:
    """DB 행 → 코드가 쓴 문장 + 장부에 등록한 출처 번호.

    register는 근거 dict(EvidenceCandidate 형식)를 받아 S#를 돌려준다 (보통 EvidenceRegistry.register).
    """
    facts = DbFacts()
    if not rows or not (items or ratio_keys):
        return facts
    aliases = load_aliases()
    scope = FS_LABEL.get(fs_div, fs_div)
    header = f"[DART 재무제표 DB · {company} · {scope} · {year}년]"

    # ① 계정값: 재무제표(sj_div)별로 묶어 근거 하나·문장 하나
    by_statement: dict[str, list[tuple[str, AccountValue, AccountValue | None]]] = {}
    for key in items:
        current = lookup(rows, key, "current")
        if current is None:
            facts.limitations.append(f"DB에서 {aliases[key].label} 값을 찾지 못함")
            continue
        by_statement.setdefault(current.sj_div, []).append((aliases[key].label, current, lookup(rows, key, "prior")))

    sentences: list[str] = []
    for sj_div, values in by_statement.items():
        title = STATEMENT_TITLE.get(sj_div, sj_div)
        rcept_no = values[0][1].rcept_no
        lines = [f"{header} {title} (접수번호 {rcept_no})"]
        lines += [f"{label} (DB 계정명: {cur.account_nm}) | 당기 {won(cur.amount)}"
                  + (f" | 전기 {won(prior.amount)}" if prior else "")
                  for label, cur, prior in values]
        evidence_id = register({
            "source_file": f"DART 재무제표 DB (접수번호 {rcept_no})",
            # 하위 주제마다 담는 계정이 달라 chunk_id에 주제를 넣는다 (같은 키면 장부가 먼저 등록된 글을 돌려준다)
            "chunk_id": f"db:{corp_code}:{year}:{fs_div}:{sj_div}:{subtopic_id}",
            "text": "\n".join(lines),
            "source_xpath": None,
            "source_format": DB_SOURCE_FORMAT,
            "title": f"{title} ({scope}, DART DB)",
        })
        facts.evidence_ids.append(evidence_id)
        parts = [f"{topic(label)} {_amount_text(cur, prior)}" for label, cur, prior in values]
        sentences.append(f"{scope} {title} 기준 {', '.join(parts)}입니다. [{evidence_id}]")

    # ② 비율: 코드 계산, 비율마다 계산 근거 하나
    for key in ratio_keys:
        name, formula, current_lines, values = _ratio_lines(rows, key)
        if current_lines is None:
            facts.limitations.append(f"{name}: 계산에 필요한 DB 값이 없어 계산하지 못함")
            continue
        evidence_id = register({
            "source_file": "코드 계산 (tools/ratios.py)",
            "chunk_id": f"calc:{corp_code}:{year}:{fs_div}:{key}",
            "text": "\n".join([f"{header} {name} = {formula} (코드 계산, 소수 첫째 자리 반올림)", *current_lines]),
            "source_xpath": None,
            "source_format": CALC_SOURCE_FORMAT,
            "title": f"{name} 계산 ({scope})",
        })
        facts.evidence_ids.append(evidence_id)
        current, prior = values
        shown = percent(current) + (f"(전기 {percent(prior)})" if prior is not None else "")
        sentences.append(f"{topic(name)} {shown}입니다. [{evidence_id}]")

    facts.summary = " ".join(sentences)
    return facts


def _ratio_lines(rows: list[Row], key: str):
    """(이름, 계산식, 근거 줄들 또는 None, (당기 값, 전기 값))."""
    if key == "return_on_equity":
        income = lookup(rows, "net_income", "current")
        opening = lookup(rows, "total_equity", "prior")
        closing = lookup(rows, "total_equity", "current")
        value = _ratio(ratios.return_on_equity, income, opening, closing)
        if value is None:
            return ROE_NAME, ROE_FORMULA, None, (None, None)
        line = (f"당기: {won(income.amount)} / (({won(opening.amount)} + {won(closing.amount)}) / 2) × 100"
                f" = {percent(value)}")
        return ROE_NAME, ROE_FORMULA, [line], (value, None)  # 전기 ROE는 전전기 자본이 없어 계산하지 않는다

    name, formula, num_key, den_key, fn = SIMPLE_RATIOS[key]
    lines, values = [], []
    for period, label in (("current", "당기"), ("prior", "전기")):
        num, den = lookup(rows, num_key, period), lookup(rows, den_key, period)
        value = _ratio(fn, num, den)
        values.append(value)
        if value is not None:
            lines.append(f"{label}: {won(num.amount)} / {won(den.amount)} × 100 = {percent(value)}")
    if values[0] is None:
        return name, formula, None, (None, None)
    return name, formula, lines, (values[0], values[1])
