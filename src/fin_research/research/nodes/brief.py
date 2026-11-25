"""brief 노드: 확정된 분석 조건 → 하위 주제별 조사 계획서(Brief).

① 하위 주제 목록·제목·필수 지표는 configs/report_template.yaml이 고정한다 (보고서 구성·평가 기준)
② LLM은 기업·업종·관심 영역에 맞는 질문과 추가 지표만 채운다 (BriefDraft)
③ build_brief가 템플릿에 맞춰 조립하고, LLM이 실패해도 기본 질문으로 진행한다
④ 노드는 Command로 Brief(update)와 1라운드 researcher Send(goto)를 함께 돌려준다
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel, Field

from fin_research.llm.client import PROJECT_ROOT
from fin_research.llm.output_parser import OutputParseError, ainvoke_structured
from fin_research.llm.router import get_llm_for_role
from fin_research.research.nodes.common import get_budget, load_prompt
from fin_research.research.nodes.supervisor import dispatch
from fin_research.research.state import Brief, ClarifyResult, ResearchState, SubTopic

TEMPLATE_PATH = PROJECT_ROOT / "configs" / "report_template.yaml"

# report_template.yaml의 id와 같아야 한다 (load_template에서 검사)
SubtopicId = Literal["profitability", "stability", "cashflow", "risk"]


class SubTopicDraft(BaseModel):
    id: SubtopicId = Field(description="템플릿의 하위 주제 id")
    questions: list[str] = Field(
        min_length=2, max_length=4,
        description="researcher가 공시를 검색해 답할 질문. 모두 기업명과 사업연도 포함",
    )
    metrics: list[str] = Field(
        default_factory=list, description="템플릿 필수 지표 외에 추가로 확인할 지표"
    )


class BriefDraft(BaseModel):
    """LLM 출력 (템플릿 조립 전 원본)."""

    objective: str = Field(min_length=1, description="보고서 목적 한두 문장")
    subtopics: list[SubTopicDraft] = Field(min_length=1, description="템플릿의 모든 하위 주제")


# ─────────────────────────── 템플릿 ───────────────────────────


@lru_cache(maxsize=1)
def load_template(path: Path = TEMPLATE_PATH) -> tuple[dict, ...]:
    """report_template.yaml의 subtopics (캐시). 형식과 SubtopicId 일치를 검사한다."""
    if not path.is_file():
        raise FileNotFoundError(f"보고서 템플릿 파일이 없습니다: {path}")
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    subtopics = (config or {}).get("subtopics")
    if not isinstance(subtopics, list) or not subtopics:
        raise ValueError(f"report_template.yaml에 'subtopics' 목록이 없습니다: {path}")

    for item in subtopics:
        missing = {"id", "title", "required_metrics", "default_questions"} - set(item or {})
        if missing:
            raise ValueError(f"템플릿 항목에 {sorted(missing)}가 없습니다: {item}")
        if len(item["default_questions"]) < 2:
            raise ValueError(f"default_questions는 2개 이상이어야 합니다: {item['id']}")
        if not isinstance(item.get("fixed_questions", False), bool):
            raise ValueError(f"fixed_questions는 true/false여야 합니다: {item['id']}")

    ids = [item["id"] for item in subtopics]
    if sorted(ids) != sorted(get_args(SubtopicId)):
        raise ValueError(
            f"템플릿 id {ids}와 brief.py의 SubtopicId {list(get_args(SubtopicId))}가 다릅니다."
        )
    return tuple(subtopics)


def format_years(years: list[int]) -> str:
    """[2025] → '2025년', [2023, 2024, 2025] → '2023~2025년', [2023, 2025] → '2023·2025년'"""
    if not years:
        return "최근 사업연도"
    ys = sorted(set(years))
    if len(ys) == 1:
        return f"{ys[0]}년"
    if ys == list(range(ys[0], ys[-1] + 1)):
        return f"{ys[0]}~{ys[-1]}년"
    return "·".join(map(str, ys)) + "년"


# ─────────────────────────── 조립 (순수 함수) ───────────────────────────


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in items if s and s.strip()))


def _with_company(question: str, company: str) -> str:
    """루프는 질문 문장 하나만 받으므로 기업명이 없으면 앞에 붙인다."""
    return question if company in question else f"{company} {question}"


def build_brief(
    draft: BriefDraft | None, clarify: ClarifyResult, template: tuple[dict, ...]
) -> Brief:
    """LLM 초안을 템플릿 순서·제목·필수 지표에 맞춰 조립. draft가 None이면 기본 질문만 사용."""
    company = clarify.company or ""
    years_text = format_years(clarify.years)

    drafted: dict[str, SubTopicDraft] = {}
    for item in (draft.subtopics if draft else []):
        drafted.setdefault(item.id, item)  # 같은 id가 여러 번이면 첫 번째만

    subtopics = []
    for spec in template:
        item = drafted.get(spec["id"])
        fixed = bool(spec.get("fixed_questions", False))
        if item and not fixed:
            questions = item.questions
            extra_metrics = item.metrics
        else:  # LLM 초안이 없거나, 템플릿 질문을 그대로 써야 하는 주제
            questions = [q.format(company=company, years=years_text) for q in spec["default_questions"]]
            extra_metrics = []
        subtopics.append(SubTopic(
            id=spec["id"],
            title=spec["title"],
            questions=[_with_company(q, company) for q in _unique(questions)],
            metrics=_unique(list(spec["required_metrics"]) + list(extra_metrics)),
            fixed_questions=fixed,
        ))

    focus_text = f" ({', '.join(clarify.focus)} 중점)" if clarify.focus else ""
    objective = (
        draft.objective.strip() if draft
        else f"{company}의 {years_text} 재무 상태를 "
             f"{'·'.join(s['title'] for s in template)} 관점에서 분석{focus_text}"
    )
    return Brief(
        company=company,
        corp_code=clarify.corp_code,
        years=clarify.years,
        objective=objective,
        subtopics=subtopics,
        focus=clarify.focus,
        notes=clarify.notes,
    )


# ─────────────────────────── 노드 ───────────────────────────


def _db_labels(spec: dict) -> list[str]:
    """DART DB에서 코드가 가져오는 숫자 이름 (brief LLM이 이 숫자는 묻지 않게)."""
    from fin_research.research.db_facts import ROE_NAME, SIMPLE_RATIOS
    from fin_research.verify.accounts import load_aliases

    aliases = load_aliases()
    ratio_names = {**{k: v[0] for k, v in SIMPLE_RATIOS.items()}, "return_on_equity": ROE_NAME}
    return ([aliases[k].label for k in spec.get("db_items") or []]
            + [ratio_names[k] for k in spec.get("db_ratios") or []])


def _brief_input(clarify: ClarifyResult, template: tuple[dict, ...]) -> str:
    return json.dumps({
        "company": clarify.company,
        "years": clarify.years,
        "focus": clarify.focus,
        "notes": clarify.notes,
        "subtopics": [
            {"id": s["id"], "title": s["title"], "required_metrics": s["required_metrics"],
             "db_metrics": _db_labels(s)}
            for s in template
        ],
    }, ensure_ascii=False, indent=2)


async def brief_node(state: ResearchState, config: RunnableConfig) -> Command[Literal["researcher"]]:
    clarify = state.get("clarify")
    if clarify is None or clarify.need_clarification or not clarify.company:
        # 라우팅 오류: clarify가 확정되지 않았는데 brief로 왔다
        raise ValueError("brief 노드는 확정된 clarify 결과가 있어야 실행됩니다.")

    template = load_template()
    llm = get_llm_for_role("brief", budget=get_budget(config))
    messages = [
        SystemMessage(load_prompt("brief")),
        HumanMessage(f"조사 조건과 하위 주제 템플릿:\n{_brief_input(clarify, template)}"),
    ]

    errors: list[str] = []
    try:
        draft = await ainvoke_structured(llm, messages, BriefDraft)
    except OutputParseError as e:
        draft = None  # 기본 질문으로 진행 (데모 중 멈추지 않게)
        errors.append(f"brief: LLM 초안 실패, 기본 질문 사용 — {e}")

    brief = build_brief(draft, clarify, template)
    update: dict = {"brief": brief, "round": 1, "step": "brief"}
    if errors:
        update["errors"] = errors
    # 하위 주제마다 researcher로 병렬 Send (1라운드)
    return Command(update=update, goto=dispatch(brief, brief.subtopics, 1))
