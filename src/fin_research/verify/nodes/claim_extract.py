"""claim_extract 노드: PDF 텍스트에서 숫자 주장을 추출한다.

① 페이지를 [p.N] 표시와 함께 묶어 LLM(claim_extractor)에 동시에 보낸다
② LLM은 주장을 '뽑기'만 한다 (ClaimExtraction). 결과는 코드가 다시 확인한다
   - ground_claim: 원문 문장이 실제로 PDF에 있는지 (지어낸 주장 제거, 쪽 번호 보정)
   - fill_value:   '32조 7,260억'처럼 LLM이 놓치기 쉬운 금액을 코드가 다시 읽기
   - dedupe:       요약·본문에 반복된 같은 주장 하나로
③ Command로 주장마다 verify_claim에 Send (주장이 없으면 aggregate로)
"""
from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal, NamedTuple

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, Send

from fin_research.companies import available_years, find_companies
from fin_research.llm.budget import BudgetExceeded, load_budget_config
from fin_research.llm.output_parser import OutputParseError, ainvoke_structured
from fin_research.llm.router import get_llm_for_role
from fin_research.research.nodes.common import get_budget, load_prompt
from fin_research.verify.numbers import UNIT_SCALE, normalize_unit, to_won
from fin_research.verify.state import (
    ClaimDraft,
    ClaimExtraction,
    ClaimVerifyInput,
    VerifyState,
    number_claims,
)

DEFAULTS = {"max_claims": 40, "max_concurrent_extract": 4, "batch_max_chars": 6000}
BUDGET_EXCEEDED_MARK = "[예산 초과]"

Page = tuple[int, str]  # (쪽 번호, 텍스트)


def verify_settings() -> dict:
    return {**DEFAULTS, **(load_budget_config().get("verify") or {})}


# ─────────────────────────── 입력 준비 (순수 함수) ───────────────────────────

def load_pages(path: str | Path) -> list[Page]:
    """PDF 또는 API가 저장한 추출 JSON({job_id}_pages.json) → 빈 쪽을 뺀 (쪽, 텍스트) 목록."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        from fin_research.ingestion.pdf_parser import extract_pdf_pages  # PyMuPDF는 필요할 때만

        data = extract_pdf_pages(path)
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list):
        raise ValueError(f"페이지 목록이 없습니다: {path}")
    return [
        (int(p["page_number"]), str(p.get("text") or ""))
        for p in pages
        if not p.get("is_blank") and str(p.get("text") or "").strip()
    ]


def batch_pages(pages: list[Page], max_chars: int = DEFAULTS["batch_max_chars"]) -> list[str]:
    """쪽마다 [p.N] 표시를 붙여 글자 수 기준으로 묶는다. 한 쪽은 중간에 자르지 않는다."""
    batches: list[str] = []
    current: list[str] = []
    size = 0
    for number, text in pages:
        block = f"[p.{number}]\n{text.strip()}"
        if current and size + len(block) > max_chars:
            batches.append("\n\n".join(current))
            current, size = [], 0
        current.append(block)
        size += len(block)
    if current:
        batches.append("\n\n".join(current))
    return batches


# ─────────────────────────── 후처리 (순수 함수, LLM을 믿지 않는 부분) ───────────────────────────

def _compact(text: str | None) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def ground_claim(draft: ClaimDraft, pages: dict[int, str]) -> ClaimDraft | None:
    """원문 문장이 그 쪽에 있으면 그대로, 다른 쪽에 있으면 쪽 번호를 고치고, 어디에도 없으면 None.

    공백·줄바꿈만 무시하고 비교한다 (PDF 줄바꿈과 LLM 띄어쓰기가 달라도 같은 문장으로 본다).
    """
    key = _compact(draft.text)
    if not key:
        return None
    if draft.page in pages and key in _compact(pages[draft.page]):
        return draft
    for number, text in pages.items():
        if key in _compact(text):
            return draft.model_copy(update={"page": number})
    return None


# '32조 7,260억', '2.2조', '△1,345억'처럼 조·억·만이 이어진 금액 하나
_AMOUNT_SPAN = re.compile(r"([△▲\-－−]?)\s*((?:\d[\d,]*(?:\.\d+)?\s*(?:조|억|만)\s*)+)")
_AMOUNT_PART = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(조|억|만)")
_PART_UNIT = {"조": "조원", "억": "억원", "만": "만원"}


class _Span(NamedTuple):
    start: int
    value: float
    unit: str  # 금액에 나온 가장 작은 단위
    parts: tuple[tuple[float, str], ...]  # '41조 845억' → ((41, '조원'), (845, '억원'))


def _amount_spans(text: str) -> list[_Span]:
    spans: list[_Span] = []
    for match in _AMOUNT_SPAN.finditer(text):
        sign, span = match.groups()
        parts = [(Decimal(n.replace(",", "")), _PART_UNIT[u]) for n, u in _AMOUNT_PART.findall(span)]
        smallest = min((u for _, u in parts), key=lambda u: UNIT_SCALE[u])
        total = sum(n * UNIT_SCALE[u] for n, u in parts) / UNIT_SCALE[smallest]
        spans.append(_Span(
            match.start(), float(-total if sign else total), smallest,
            tuple((float(-n if sign else n), u) for n, u in parts),
        ))
    return spans


def korean_amounts(text: str) -> list[tuple[float, str]]:
    """문장 속 조·억·만 금액들을 (값, 단위)로.

    '32조 7,260억' → (327260, '억원'): 정밀도(±0.5억)를 잃지 않으려고 원으로 바꾸지 않는다.
    """
    return [(s.value, s.unit) for s in _amount_spans(text)]


def amount_for_item(text: str, item: str) -> tuple[float, str] | None:
    """문장에서 이 항목의 금액. 금액이 하나면 그것, 여럿이면 항목 이름 바로 뒤의 첫 금액.

    '자산총계는 41조 845억원, 부채총계는 12.1조원' + '부채총계' → (12.1, '조원').
    항목 이름이 문장에 없으면(매출액 ↔ '영업수익(매출)') 정할 수 없어 None.
    """
    spans = _amount_spans(text)
    if len(spans) == 1:
        return spans[0].value, spans[0].unit
    position = text.find(item.strip()) if item and item.strip() else -1
    if position < 0:
        return None
    after = [s for s in spans if s.start >= position + len(item.strip())]
    return (after[0].value, after[0].unit) if after else None


def _whole_amount(value: float | None, unit: str | None, spans: list[_Span]) -> tuple[float, str] | None:
    """LLM 값이 섞인 금액의 한 조각이면 전체 금액. '41조 845억' 중 (41, 조원)이나 (845, 억원) → (410845, 억원)."""
    canonical = normalize_unit(unit)
    for span in spans:
        if len(span.parts) > 1 and (value, canonical) in span.parts:
            return span.value, span.unit
    return None


def fill_value(draft: ClaimDraft) -> ClaimDraft:
    """문장에서 이 항목의 조·억·만 금액을 코드가 정할 수 있으면 그 값과 단위를 쓴다.

    - LLM이 value를 비웠으면 채운다 ('32조 7,260억'은 프롬프트에서 null로 두라고 했다)
    - LLM 값이 다르면 바로잡는다 (환산 실수, 자릿수 누락)
    - 값이 같아도 단위는 원문 단위로 되돌린다: LLM이 2.2조를 2.2e12원으로 바꾸면
      허용 오차가 ±0.05조에서 ±0.5원이 되어 반올림한 주장이 모두 불일치가 된다
    - 금액이 여러 개인데 항목 이름으로 정할 수 없는 문장, 비율(%)은 LLM 값을 그대로 둔다
      ('영업이익은 2.2조원으로 전년 1.98조원 대비'의 전기 주장 1.98을 2.2로 덮어쓰지 않도록,
       금액이 여럿일 때는 LLM 값이 비어 있을 때만 채운다)
    """
    # 금액 단위이거나 값이 비어 있을 때만 바로잡는다. 단위 없는 숫자(비율일 수 있음)는 건드리지 않는다.
    if normalize_unit(draft.unit) not in UNIT_SCALE and draft.value is not None:
        return draft
    spans = _amount_spans(draft.text)
    # LLM이 '41조 845억'을 41조와 845억 두 주장으로 쪼갠 경우 → 둘 다 전체 금액으로 (dedupe가 하나로 합친다)
    found = _whole_amount(draft.value, draft.unit, spans)
    if found is None and len(spans) > 1 and draft.value is not None:
        return draft
    found = found or amount_for_item(draft.text, draft.item)
    if found is None:
        return draft
    value, unit = found
    if draft.value == value and normalize_unit(draft.unit) == unit:
        return draft
    return draft.model_copy(update={"value": value, "unit": unit})


def dedupe(drafts: list[ClaimDraft]) -> list[ClaimDraft]:
    """같은 (항목, 원 단위 값, 연도, 연결/별도, 기간)은 처음 나온 하나만 남긴다."""
    seen: set[tuple] = set()
    kept: list[ClaimDraft] = []
    for draft in drafts:
        won = to_won(draft.value, draft.unit)
        key = (_compact(draft.item), round(won, 2) if won is not None else draft.text,
               draft.year, draft.scope, draft.period)
        if key not in seen:
            seen.add(key)
            kept.append(draft)
    return kept


def postprocess(drafts: list[ClaimDraft], pages: list[Page], max_claims: int) -> tuple[list[ClaimDraft], list[str]]:
    """ground → fill → dedupe → 개수 제한. (남은 주장, 안내 메시지)."""
    by_page = dict(pages)
    grounded = [g for g in (ground_claim(d, by_page) for d in drafts) if g is not None]
    notes: list[str] = []
    if len(grounded) < len(drafts):
        notes.append(f"claim_extract: 원문에서 찾지 못한 주장 {len(drafts) - len(grounded)}개를 제외했습니다.")
    unique = dedupe([fill_value(d) for d in grounded])
    if len(unique) > max_claims:
        notes.append(f"claim_extract: 주장 {len(unique)}개 중 앞의 {max_claims}개만 검증합니다.")
        unique = unique[:max_claims]
    return unique, notes


# ─────────────────────────── 기업 확정 ───────────────────────────

def resolve_company(name: str | None) -> tuple[dict | None, str | None]:
    """(기업, 오류). 이번 주는 입력(company)으로 받는다. PDF에서 찾기는 3주차."""
    if not name or not name.strip():
        return None, "claim_extract: 검증할 기업(company)이 지정되지 않았습니다."
    candidates = find_companies(name)
    if len(candidates) != 1:
        found = ", ".join(c["name"] for c in candidates) or "없음"
        return None, f"claim_extract: 기업 '{name}'을 하나로 정할 수 없습니다 (후보: {found})."
    if not available_years(candidates[0]):
        return None, f"claim_extract: {candidates[0]['name']}은 수집된 사업보고서가 없습니다."
    return candidates[0], None


# ─────────────────────────── 노드 ───────────────────────────

def _stop(errors: list[str], **update) -> Command:
    """검증할 주장이 없으면 aggregate로 가서 빈 요약이라도 만든다."""
    return Command(update={"claims": [], "errors": errors, "step": "claim_extract", **update}, goto="aggregate")


async def _extract_batches(llm, system: str, company: str, batches: list[str], concurrency: int):
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(batch: str) -> ClaimExtraction:
        async with semaphore:
            messages = [SystemMessage(system), HumanMessage(f"기업: {company}\n\n보고서 내용:\n{batch}")]
            return await ainvoke_structured(llm, messages, ClaimExtraction)

    return await asyncio.gather(*(run(b) for b in batches), return_exceptions=True)


async def claim_extract_node(
    state: VerifyState, config: RunnableConfig
) -> Command[Literal["verify_claim", "aggregate"]]:
    company, error = resolve_company(state.get("company"))
    if error:
        return _stop([error])
    fiscal_year = max(available_years(company))
    rcept_no = ((company.get("reports") or {}).get(str(fiscal_year)) or {}).get("rcept_no")
    target = {"company": company["name"], "corp_code": company["corp_code"],
              "fiscal_year": fiscal_year, "rcept_no": rcept_no}

    try:
        pages = load_pages(state["pdf_path"])
    except Exception as e:  # PDF 손상·경로 오류·JSON 형식 오류 모두 사용자에게 알리고 멈춘다
        return _stop([f"claim_extract: PDF를 읽지 못했습니다 ({type(e).__name__}: {e})"], **target)
    if not pages:
        return _stop(["claim_extract: 텍스트가 있는 쪽이 없습니다 (스캔 PDF일 수 있습니다)."], **target)

    settings = verify_settings()
    llm = get_llm_for_role("claim_extractor", budget=get_budget(config))
    results = await _extract_batches(
        llm, load_prompt("claim_extract", folder="verify"), company["name"],
        batch_pages(pages, settings["batch_max_chars"]), settings["max_concurrent_extract"],
    )

    drafts: list[ClaimDraft] = []
    errors: list[str] = []
    for index, result in enumerate(results, start=1):
        if isinstance(result, ClaimExtraction):
            drafts.extend(result.claims)
        elif isinstance(result, BudgetExceeded):
            # 이미 뽑은 주장은 살리고, 예산 초과는 결과에 남긴다
            errors.append(f"claim_extract: {BUDGET_EXCEEDED_MARK} {index}번째 묶음을 처리하지 못했습니다 ({result})")
        elif isinstance(result, OutputParseError):
            errors.append(f"claim_extract: {index}번째 묶음 응답을 해석하지 못했습니다 ({result})")
        else:
            raise result  # 예상하지 못한 오류(네트워크 등)는 숨기지 않는다

    kept, notes = postprocess(drafts, pages, settings["max_claims"])
    claims = number_claims(kept)
    sends = [
        Send("verify_claim", ClaimVerifyInput(claim=claim, corp_code=target["corp_code"],
                                              fiscal_year=fiscal_year, rcept_no=rcept_no))
        for claim in claims
    ]
    return Command(
        update={"claims": claims, "errors": errors + notes, "step": "claim_extract", **target},
        goto=sends or "aggregate",
    )
