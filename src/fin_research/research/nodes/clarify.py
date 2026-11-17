"""clarify 노드: 요청 문장 → 확정된 분석 조건.

① LLM은 요청에서 회사·기간·관심 영역을 '추출'만 한다 (ClarifyExtraction)
② 확정·되묻기 판단은 코드가 companies.yaml과 대조해 결정한다 (validate_request)
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from pydantic import BaseModel, Field

from fin_research.companies import available_years, find_companies, supported_names
from fin_research.llm.output_parser import OutputParseError, ainvoke_structured
from fin_research.llm.router import get_llm_for_role
from fin_research.research.nodes.common import get_budget, load_prompt
from fin_research.research.state import ClarifyResult, ResearchState

MAX_LISTED = 8  # 질문에 보여줄 기업 이름 수


class ClarifyExtraction(BaseModel):
    """LLM 추출 결과 (검증 전 원본)."""

    company_mentions: list[str] = Field(
        description="요청에 등장한 회사 이름을 적힌 그대로. 조사·어미는 빼고, 없으면 빈 목록"
    )
    explicit_years: list[int] = Field(
        description="요청에 숫자로 명시된 사업연도만. 예: '2024년' → [2024]. 없으면 빈 목록"
    )
    recent_n_years: int | None = Field(
        description="'최근 N년'·'지난 N년' 표현이면 N. 없으면 null"
    )
    focus: list[str] = Field(
        description="사용자가 특히 요청한 분석 영역. 예: 수익성, 부채, 현금흐름. 없으면 빈 목록"
    )


# ─────────────────────────── 검증 (순수 함수) ───────────────────────────


def _ask(question: str) -> ClarifyResult:
    return ClarifyResult(need_clarification=True, question=question)


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_LISTED])
    return shown + (f" 외 {len(names) - MAX_LISTED}개" if len(names) > MAX_LISTED else "")


def resolve_years(
    extraction: ClarifyExtraction, available: list[int]
) -> tuple[list[int], list[str], str | None]:
    """(분석 연도, 안내 메모, 되물을 질문). 기준은 오늘이 아니라 수집된 최신 사업연도."""
    if not available:
        return [], [], "이 기업은 아직 수집된 사업보고서가 없습니다."

    if extraction.explicit_years:
        requested = sorted(set(extraction.explicit_years))
    elif extraction.recent_n_years and extraction.recent_n_years > 0:
        latest = max(available)
        requested = list(range(latest - extraction.recent_n_years + 1, latest + 1))
    else:
        requested = list(available)  # 기간 언급 없음 → 수집된 연도 전부

    years = [y for y in requested if y in available]
    missing = [y for y in requested if y not in available]
    if not years:
        return [], [], (
            f"{', '.join(map(str, requested))}년 자료가 없습니다. "
            f"분석 가능한 사업연도: {', '.join(map(str, available))}"
        )
    notes = (
        [f"{', '.join(map(str, missing))}년 자료가 없어 {', '.join(map(str, years))}년만 분석합니다."]
        if missing else []
    )
    return years, notes, None


def validate_request(extraction: ClarifyExtraction) -> ClarifyResult:
    """추출 결과를 companies.yaml과 대조해 확정하거나 질문을 만든다."""
    mentions = list(dict.fromkeys(m.strip() for m in extraction.company_mentions if m.strip()))
    if not mentions:
        return _ask(f"어느 기업을 분석할까요? (예: {_listed(supported_names())})")

    # 언급마다 후보를 찾고, 같은 기업을 가리키는 언급은 하나로 본다 ('삼성전자', '삼성전자㈜')
    resolved: dict[str, dict] = {}
    for mention in mentions:
        candidates = find_companies(mention)
        if not candidates:
            return _ask(
                f"'{mention}'은(는) 분석 대상 기업이 아닙니다. "
                f"지원 기업: {_listed(supported_names())}"
            )
        if len(candidates) > 1:
            return _ask(
                f"'{mention}'에 해당하는 기업이 여러 개입니다: "
                f"{_listed([c['name'] for c in candidates])}. 어느 기업인가요?"
            )
        resolved[candidates[0]["corp_code"]] = candidates[0]

    if len(resolved) > 1:
        names = [c["name"] for c in resolved.values()]
        return _ask(f"한 번에 한 기업만 분석할 수 있습니다: {', '.join(names)} 중 어느 기업인가요?")

    company = next(iter(resolved.values()))
    years, notes, question = resolve_years(extraction, available_years(company))
    if question:
        return _ask(question)

    return ClarifyResult(
        need_clarification=False,
        company=company["name"],
        corp_code=company["corp_code"],
        years=years,
        focus=extraction.focus,
        notes=notes,
    )


# ─────────────────────────── 노드 ───────────────────────────


async def clarify_node(state: ResearchState, config: RunnableConfig) -> dict:
    request = (state.get("request") or "").strip()
    if not request:
        return {"clarify": _ask("분석할 기업과 기간을 알려주세요. (예: 삼성전자 최근 3년 재무 분석)"),
                "step": "clarify"}

    llm = get_llm_for_role("clarify", budget=get_budget(config))
    messages = [
        SystemMessage(load_prompt("clarify")),
        HumanMessage(f"사용자 요청:\n{request}"),
    ]
    try:
        extraction = await ainvoke_structured(llm, messages, ClarifyExtraction)
    except OutputParseError as e:
        return {
            "clarify": _ask("요청을 이해하지 못했습니다. 기업명과 기간을 다시 알려주세요."),
            "step": "clarify",
            "errors": [f"clarify: {e}"],
        }
    return {"clarify": validate_request(extraction), "step": "clarify"}


def route_after_clarify(state: ResearchState) -> str:
    """되물어야 하면 그래프 종료(API가 question을 사용자에게 전달), 아니면 brief로."""
    return END if state["clarify"].need_clarification else "brief"