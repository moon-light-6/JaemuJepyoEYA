"""supervisor: 하위 주제를 researcher에게 병렬 분배하고, 부족한 주제만 한 번 더 조사시킨다.

① dispatch: 하위 주제마다 researcher로 Send (brief가 1라운드, supervisor_review가 재조사 때 사용)
② supervisor_review: 부족한 주제·라운드·예산을 '코드'로 판단하고
   Command(update=..., goto=...)로 다음 행선지(재조사 Send 또는 종료)까지 직접 정한다
③ 재조사할 때만 LLM(supervisor 역할)이 1라운드 한계를 보고 질문을 다시 쓴다
   (같은 질문을 다시 보내면 같은 검색 결과가 나오므로)
"""

from __future__ import annotations

import json
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, Send
from pydantic import BaseModel, Field

from fin_research.llm.budget import BudgetExceeded, load_budget_config
from fin_research.llm.output_parser import OutputParseError, ainvoke_structured
from fin_research.llm.router import get_llm_for_role
from fin_research.research.nodes.common import get_budget, load_prompt
from fin_research.research.nodes.researcher import BUDGET_EXCEEDED_MARK
from fin_research.research.state import (
    Brief,
    Finding,
    ResearcherInput,
    ResearchState,
    SubTopic,
    SupervisorDecision,
)


class RetryItem(BaseModel):
    id: str = Field(description="다시 조사할 하위 주제 id")
    questions: list[str] = Field(
        min_length=1, max_length=3,
        description="이전과 다른 각도의 새 질문. 모두 기업명과 사업연도 포함",
    )


class RetryQuestions(BaseModel):
    subtopics: list[RetryItem] = Field(min_length=1)


def max_rounds(config: RunnableConfig | None) -> int:
    """config["configurable"]["max_rounds"]가 있으면 그 값, 없으면 budget.yaml research.max_rounds."""
    override = ((config or {}).get("configurable") or {}).get("max_rounds")
    return int(override or (load_budget_config().get("research") or {}).get("max_rounds") or 2)


# ─────────────────────────── 분배 ───────────────────────────


def dispatch(brief: Brief, subtopics: list[SubTopic], round_: int) -> list[Send]:
    """하위 주제마다 researcher로 Send. Command(goto=...)에 그대로 넣는다."""
    return [
        Send("researcher", ResearcherInput(subtopic=s, brief=brief, round=round_))
        for s in subtopics
    ]


# ─────────────────────────── 검토 ───────────────────────────


def latest_by_subtopic(findings: list[Finding]) -> dict[str, Finding]:
    """하위 주제별로 가장 최근 라운드의 Finding."""
    latest: dict[str, Finding] = {}
    for f in findings:
        if f.subtopic_id not in latest or f.round >= latest[f.subtopic_id].round:
            latest[f.subtopic_id] = f
    return latest


def _budget_exhausted(latest: dict[str, Finding], budget) -> bool:
    if any(BUDGET_EXCEEDED_MARK in note for f in latest.values() for note in f.limitations):
        return True
    if budget is not None:
        try:
            budget.check()
        except BudgetExceeded:
            return True
    return False


def _with_company(question: str, company: str) -> str:
    return question if company in question else f"{company} {question}"


def rotate_questions(subtopic: SubTopic) -> SubTopic:
    """질문을 바꿀 수 없는 주제: 순서만 한 칸 돌려 재조사 때 아직 안 쓴 질문이 먼저 오게 한다.

    researcher는 앞에서부터 questions_per_subtopic개만 쓰므로, ['Q1','Q2'] → ['Q2','Q1'].
    """
    q = subtopic.questions
    return subtopic.model_copy(update={"questions": q[1:] + q[:1]}) if len(q) > 1 else subtopic


async def rewrite_questions(
    lacking: list[SubTopic], latest: dict[str, Finding], brief: Brief, config: RunnableConfig
) -> tuple[list[SubTopic], list[str]]:
    """부족한 하위 주제의 질문을 LLM으로 다시 쓴다. 실패하거나 빠진 주제는 기존 질문 그대로.

    fixed_questions 주제는 LLM에 넘기지 않고 순서만 돌린다 (공시 용어가 흐려지지 않게).
    """
    rewritable = [s for s in lacking if not s.fixed_questions]
    errors: list[str] = []
    rewritten: dict[str, list[str]] = {}
    if not rewritable:  # 모두 고정 질문이면 LLM을 부르지 않는다
        return [rotate_questions(s) for s in lacking], errors
    payload = {
        "company": brief.company,
        "years": brief.years,
        "subtopics": [
            {
                "id": s.id, "title": s.title, "metrics": s.metrics,
                "previous_questions": s.questions,
                "previous_limitations": latest[s.id].limitations if s.id in latest else ["결과 없음"],
            }
            for s in rewritable
        ],
    }
    try:
        llm = get_llm_for_role("supervisor", budget=get_budget(config))
        result = await ainvoke_structured(llm, [
            SystemMessage(load_prompt("supervisor")),
            HumanMessage(f"재조사할 하위 주제:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"),
        ], RetryQuestions)
        allowed = {s.id for s in rewritable}
        for item in result.subtopics:
            if item.id in allowed and item.id not in rewritten:
                rewritten[item.id] = [_with_company(q, brief.company) for q in item.questions]
    except (OutputParseError, BudgetExceeded) as e:
        errors.append(f"supervisor: 질문 재작성 실패, 기존 질문으로 재조사 — {e}")

    return [
        rotate_questions(s) if s.fixed_questions
        else s.model_copy(update={"questions": rewritten[s.id]}) if s.id in rewritten
        else s
        for s in lacking
    ], errors


async def supervisor_review(
    state: ResearchState, config: RunnableConfig
) -> Command[Literal["researcher", "outline"]]:
    brief = state["brief"]
    round_ = state.get("round", 1)
    latest = latest_by_subtopic(state.get("findings") or [])
    lacking = [
        s for s in brief.subtopics
        if s.id not in latest or latest[s.id].status == "insufficient_evidence"
    ]

    def stop(complete: bool, reason: str) -> Command:
        # 조사를 끝내고 보고서 작성(outline → writer)으로
        return Command(
            update={
                "supervisor_decision": SupervisorDecision(complete=complete, reason=reason),
                "step": f"supervisor_review:round{round_}",
            },
            goto="outline",
        )

    if not lacking:
        return stop(True, f"{round_}라운드에서 모든 하위 주제 조사 완료")
    lacking_ids = [s.id for s in lacking]
    if round_ >= max_rounds(config):
        return stop(False, f"최대 {round_}라운드 도달, 부족한 주제: {', '.join(lacking_ids)}")
    if _budget_exhausted(latest, get_budget(config)):
        return stop(False, f"예산 소진으로 재조사 중단, 부족한 주제: {', '.join(lacking_ids)}")

    retry_subtopics, errors = await rewrite_questions(lacking, latest, brief, config)
    kept = [f for f in latest.values() if f.status == "draft"]
    update: dict = {
        "supervisor_decision": SupervisorDecision(
            complete=False,
            retry_subtopic_ids=lacking_ids,
            retry_subtopics=retry_subtopics,
            reason=f"{round_}라운드 부족한 주제 재조사: {', '.join(lacking_ids)}",
        ),
        # 실패한 결과는 지우고 성공한 것만 남긴 뒤 재조사 결과를 이어 붙인다
        "findings": {"type": "override", "value": kept},
        "round": round_ + 1,
        "step": f"supervisor_review:round{round_}",
    }
    if errors:
        update["errors"] = errors
    # 재조사 대상만 다음 라운드 번호로 다시 보낸다
    return Command(update=update, goto=dispatch(brief, retry_subtopics, round_ + 1))
