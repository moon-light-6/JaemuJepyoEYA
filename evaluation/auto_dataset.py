"""OpenDART 재무제표 API 값과 PDF 텍스트를 대조해 검색 평가셋을 자동으로 만든다.

사람이 PDF를 보고 Gold page를 적는 대신, OpenDART 단일회사 전체 재무제표 API
(fnlttSinglAcntAll)의 계정 금액을 PDF 단위(원/천원/백만원)로 바꾼 뒤, 그 숫자가
실제로 적힌 재무제표 페이지를 Gold page로 기록한다. 숫자가 PDF에서 확인되지 않은
계정은 문항을 만들지 않는다.

자동으로 만들 수 있는 것은 수치형(numeric)·비교형(comparison) 문항뿐이다.
주석 세부(note_detail)·서술형(narrative) 문항은 사람이 따로 추가해야 한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fin_research.ingestion.section_detector import UNIT_SCALE, SectionPlan


ANNOTATION_METHOD = "auto_opendart_pdf_match"


@dataclass(frozen=True)
class AccountSpec:
    key: str
    label: str          # 질문에 들어갈 계정 이름 (조사 포함)
    sj_divs: tuple[str, ...]
    account_ids: tuple[str, ...]
    names: tuple[str, ...]
    category: str


ACCOUNT_SPECS = {
    spec.key: spec
    for spec in [
        AccountSpec("assets", "자산총계는", ("BS",), ("ifrs-full_Assets",), ("자산총계",), "financial_position"),
        AccountSpec("cash", "현금및현금성자산은", ("BS",), ("ifrs-full_CashAndCashEquivalents",), ("현금및현금성자산",), "financial_position"),
        AccountSpec("inventories", "재고자산은", ("BS",), ("ifrs-full_Inventories",), ("재고자산",), "financial_position"),
        AccountSpec("liabilities", "부채총계는", ("BS",), ("ifrs-full_Liabilities",), ("부채총계",), "financial_position"),
        AccountSpec("equity", "자본총계는", ("BS",), ("ifrs-full_Equity",), ("자본총계",), "financial_position"),
        AccountSpec("revenue", "매출액은", ("IS", "CIS"), ("ifrs-full_Revenue",), ("매출액", "수익(매출액)", "매출", "영업수익"), "income_statement"),
        AccountSpec("operating_income", "영업이익은", ("IS", "CIS"), ("dart_OperatingIncomeLoss",), ("영업이익", "영업이익(손실)", "영업손익", "영업손실"), "income_statement"),
        AccountSpec("net_income", "당기순이익은", ("IS", "CIS"), ("ifrs-full_ProfitLoss",), ("당기순이익", "당기순이익(손실)", "당기순손익", "당기순손실", "연결당기순이익"), "income_statement"),
        AccountSpec("operating_cf", "영업활동으로 인한 현금흐름은", ("CF",), ("ifrs-full_CashFlowsFromUsedInOperatingActivities",), ("영업활동현금흐름", "영업활동으로인한현금흐름", "영업활동으로인한순현금흐름"), "cash_flow"),
        AccountSpec("dividends_paid", "배당금 지급액은", ("CF",), ("ifrs-full_DividendsPaidClassifiedAsFinancingActivities",), ("배당금의지급", "배당금지급", "배당금의지급액"), "cash_flow"),
    ]
}

# 삼성전자 평가셋(S01~S12)과 같은 순서. 값을 못 찾으면 뒤의 예비 계정으로 채운다.
NUMERIC_ORDER = [
    "assets", "cash", "inventories", "revenue", "operating_income",
    "net_income", "operating_cf", "dividends_paid", "liabilities", "equity",
]
COMPARISON_ORDER = [
    "assets", "revenue", "operating_income", "dividends_paid",
    "net_income", "equity", "liabilities", "operating_cf",
]

SCOPE_LABEL = {"consolidated": "연결", "separate": "별도"}


def parse_amount(value: object) -> int | None:
    """OpenDART 금액 문자열('1,234', '-5678', '')을 정수로 바꾼다."""

    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not re.fullmatch(r"-?\d+", text):
        return None
    return int(text)


def scaled_digits(amount: int, unit: str) -> str | None:
    """원 단위 금액을 PDF 단위의 쉼표 숫자로 바꾼다(반올림). 너무 짧은 숫자는 오탐 위험으로 제외."""

    scale = UNIT_SCALE[unit]
    value = (abs(amount) + scale // 2) // scale
    if value < 1_000:
        return None
    return f"{value:,}"


def display_value(amount: int, unit: str) -> str:
    scale = UNIT_SCALE[unit]
    digits = f"{(abs(amount) + scale // 2) // scale:,}"
    return f"({digits})" if amount < 0 else digits


def _normalize(name: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"\(주[\d,\s]*\)", "", name or ""))


def select_account(accounts: list[dict], spec: AccountSpec) -> dict | None:
    """계정 ID → 계정명 순서로 OpenDART 계정 한 건을 고른다."""

    for sj_div in spec.sj_divs:
        rows = [row for row in accounts if row.get("sj_div") == sj_div]
        for row in rows:
            if row.get("account_id") in spec.account_ids:
                return row
        names = {_normalize(name) for name in spec.names}
        for row in rows:
            if _normalize(row.get("account_nm", "")) in names:
                return row
    return None


NUMERIC_LINE = re.compile(r"^[\s　|]*(\(?-?[\d,]+\)?|-)?[\s　|]*$")


def _number_pattern(digits: str) -> re.Pattern:
    return re.compile(r"(?<![\d,.])" + re.escape(digits) + r"(?![\d]|[,.]\d)")


def _label_key(text: str) -> str:
    text = re.sub(r"\(주[\d,\s]*\)", "", text)
    text = re.sub(r"[\s　|ㆍ·]", "", text)
    return re.sub(r"^([IVXⅠ-Ⅹ]+\.|\d+\.|[가-하]\.)", "", text)


def pages_with_number(page_map: dict[int, str], pages: list[int], digits: str) -> list[int]:
    pattern = _number_pattern(digits)
    return [page for page in pages if pattern.search(page_map.get(page, ""))]


def pages_with_labeled_number(
    page_map: dict[int, str], pages: list[int], digits: str, names: set[str]
) -> list[int]:
    """숫자 바로 앞(표의 같은 행)의 계정명이 names 중 하나인 페이지만 고른다.

    PDF 표 텍스트는 '계정명 줄 → 당기 → 전기 → 전전기' 순서로 추출된다. 숫자 줄에서
    위로 올라가며 처음 나오는 숫자가 아닌 줄을 그 행의 계정명으로 본다.
    예) '부채와자본총계' 행의 566,942,110은 자산총계 문항의 Gold page로 쓰지 않는다.
    """

    pattern = _number_pattern(digits)
    keys = {_label_key(name) for name in names}
    found: list[int] = []
    for page in pages:
        lines = page_map.get(page, "").splitlines()
        for index, line in enumerate(lines):
            match = pattern.search(line)
            if not match:
                continue
            same_line = _label_key(line[: match.start()])
            if same_line and any(same_line.startswith(key) for key in keys):
                found.append(page)
                break
            label = None
            for previous in reversed(lines[max(0, index - 4):index]):
                if not NUMERIC_LINE.match(previous):
                    label = _label_key(previous)
                    break
            if label in keys:
                found.append(page)
                break
    return found


def find_gold_pages(
    page_map: dict[int, str], pages: list[int], digits: str, names: set[str]
) -> list[int]:
    labeled = pages_with_labeled_number(page_map, pages, digits, names)
    if labeled:
        return labeled
    # 계정명을 확인하지 못하면 숫자가 처음 나온 페이지 하나만 쓴다.
    return pages_with_number(page_map, pages, digits)[:1]


def _names(spec: AccountSpec, row: dict) -> set[str]:
    return set(spec.names) | {row.get("account_nm") or ""}


def _candidate_pages(plan: SectionPlan, spec: AccountSpec, row: dict) -> list[int]:
    """계정이 속한 재무제표(재무상태표·손익계산서·현금흐름표)의 페이지."""

    sj_div = row.get("sj_div") or spec.sj_divs[0]
    fallbacks = {"IS": ("IS", "CIS"), "CIS": ("CIS", "IS")}.get(sj_div, (sj_div,))
    for kind in fallbacks:
        pages = plan.statement_pages.get(kind)
        if pages:
            return pages
    statements = plan.statements_range
    return list(range(statements.start_page, plan.notes_range.start_page + 1))


def build_auto_eval_set(
    accounts: list[dict],
    page_map: dict[int, str],
    plan: SectionPlan,
    source_file: str,
    fiscal_year: int,
    numeric_count: int = 8,
    comparison_count: int = 4,
) -> tuple[list[dict], list[str]]:
    """수치형·비교형 평가 문항과 건너뛴 계정 사유를 돌려준다."""

    if plan.unit not in UNIT_SCALE:
        raise ValueError(f"재무제표 단위를 확인하지 못했습니다: {plan.unit}")

    unit = plan.unit
    scope = SCOPE_LABEL[plan.statement_scope]
    previous_year = fiscal_year - 1
    items: list[dict] = []
    skipped: list[str] = []

    def gold(pages: list[int]) -> list[dict]:
        return [{"source_file": source_file, "pdf_page": page} for page in pages]

    def account_info(row: dict) -> dict:
        return {
            "sj_div": row.get("sj_div"),
            "account_id": row.get("account_id"),
            "account_nm": row.get("account_nm"),
        }

    # 1) 수치형: 당기 금액이 적힌 재무제표 페이지
    for key in NUMERIC_ORDER:
        if sum(item["answer_type"] == "numeric" for item in items) >= numeric_count:
            break
        spec = ACCOUNT_SPECS[key]
        row = select_account(accounts, spec)
        amount = parse_amount(row.get("thstrm_amount")) if row else None
        digits = scaled_digits(amount, unit) if amount else None
        if not digits:
            skipped.append(f"{spec.label[:-1]}: OpenDART 값 없음 또는 비교 불가")
            continue
        pages = find_gold_pages(page_map, _candidate_pages(plan, spec, row), digits, _names(spec, row))
        if not pages:
            skipped.append(f"{spec.label[:-1]}: {digits}을(를) 재무제표 페이지에서 찾지 못함")
            continue
        items.append({
            "id": f"A{len(items) + 1:02d}",
            "question": f"{fiscal_year}년 {scope} 기준 {spec.label} 얼마인가?",
            "answer_type": "numeric",
            "expected_value": display_value(amount, unit),
            "unit": unit,
            "gold_sources": gold(pages),
            "category": spec.category,
            "annotation_status": "verified",
            "annotation_method": ANNOTATION_METHOD,
            "opendart_account": account_info(row),
        })

    # 2) 비교형: 당기·전기 금액이 함께 적힌 페이지
    comparisons = 0
    for key in COMPARISON_ORDER:
        if comparisons >= comparison_count:
            break
        spec = ACCOUNT_SPECS[key]
        row = select_account(accounts, spec)
        if not row:
            continue
        current = parse_amount(row.get("thstrm_amount"))
        previous = parse_amount(row.get("frmtrm_amount"))
        current_digits = scaled_digits(current, unit) if current else None
        previous_digits = scaled_digits(previous, unit) if previous else None
        if not (current_digits and previous_digits):
            continue
        candidates = _candidate_pages(plan, spec, row)
        names = _names(spec, row)
        current_pages = set(find_gold_pages(page_map, candidates, current_digits, names))
        pages = [
            page
            for page in find_gold_pages(page_map, candidates, previous_digits, names)
            if page in current_pages
        ]
        if not pages:
            skipped.append(f"{spec.label[:-1]} 비교: 당기·전기 금액이 함께 있는 페이지 없음")
            continue
        comparisons += 1
        items.append({
            "id": f"A{len(items) + 1:02d}",
            "question": f"{fiscal_year}년과 {previous_year}년 {scope} 기준 {spec.label} 각각 얼마인가?",
            "answer_type": "comparison",
            "expected_value": {
                str(fiscal_year): display_value(current, unit),
                str(previous_year): display_value(previous, unit),
            },
            "unit": unit,
            "gold_sources": gold(pages),
            "category": spec.category,
            "annotation_status": "verified",
            "annotation_method": ANNOTATION_METHOD,
            "opendart_account": account_info(row),
        })

    return items, skipped
