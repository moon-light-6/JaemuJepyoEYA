"""Deep Research 그래프의 state와 노드 간 데이터 구조.

- 내용물(LLM 출력·노드 간 데이터)은 Pydantic → invoke_structured로 바로 검증
- 그래프 state는 TypedDict → 노드는 바꾼 칸만 dict로 반환
- Budget, LLM 객체, 근거 원문은 state에 넣지 않는다 (직렬화 불가·용량)
  · Budget·실행 설정 → config["configurable"]
  · 근거 원문 → Evidence Registry, state에는 ID만
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


# ─────────────────────────────────────────────
# 리듀서
# ─────────────────────────────────────────────

def override_reducer(current: list | None, new: Any) -> list:
    """기본은 이어 붙이기, {"type": "override", "value": [...]}이면 통째로 교체.

    - 병렬 researcher 결과 모으기:  {"findings": [f1]}
    - 2라운드 재분배 전 초기화:     {"findings": {"type": "override", "value": []}}
    """
    if isinstance(new, dict) and new.get("type") == "override":
        return list(new.get("value") or [])
    return (current or []) + (new or [])


# ─────────────────────────────────────────────
# 내용물 (Pydantic)
# ─────────────────────────────────────────────

class ClarifyResult(BaseModel):
    """clarify 노드 출력. 1차 구현은 LLM 질문 대신 검증·정규화."""

    need_clarification: bool = Field(description="사용자에게 되물어야 하면 True")
    question: str = Field("", description="need_clarification일 때 사용자에게 보낼 질문")
    company: str | None = Field(None, description="정규화된 기업명 (companies.yaml 기준)")
    corp_code: str | None = Field(None, description="DART 기업 고유번호 8자리")
    years: list[int] = Field(default_factory=list, description="분석 대상 사업연도")
    focus: list[str] = Field(default_factory=list, description="사용자가 특히 요청한 관심 영역")
    notes:list[str]=Field(default_factory=list, description='진행은 하지만 알려야 할 사항. 예: 일부 연도 자료 없음')

class SubTopic(BaseModel):
    """researcher 하나가 맡는 하위 주제. (open_deep_research의 ConductResearch 대신)"""

    id: str = Field(description="영문 소문자 식별자. 예: profitability")
    title: str = Field(description="보고서 섹션 제목. 예: 수익성")
    questions: list[str] = Field(description="researcher가 답해야 할 구체 질문 2~4개")
    metrics: list[str] = Field(default_factory=list, description="확인할 지표. 예: 매출액, 영업이익률")


class Brief(BaseModel):
    """brief 노드 출력: 조사 계획서. (open_deep_research의 ResearchQuestion을 구조화)"""

    company: str
    corp_code: str | None = None
    years: list[int]
    objective: str = Field(description="보고서 목적 한두 문장")
    subtopics: list[SubTopic] = Field(min_length=1)
    focus: list[str] = Field(default_factory=list, description="사용자가 특히 요청한 관심 영역")
    notes: list[str] = Field(default_factory=list, description="조사 범위의 한계. 예: 2023~2024년 자료 없음")


class Finding(BaseModel):
    """researcher 1회 결과 (researcher 안에서 압축 완료된 것).

    루프의 GroundedAnswer와 필드를 맞춰, 루프 결과를 거의 그대로 받는다.
    """

    subtopic_id: str
    status: Literal["draft", "insufficient_evidence"]
    summary: str = Field(description="한국어 조사 결과. 근거를 쓴 문장마다 [S#] 표기")
    evidence_ids: list[str] = Field(default_factory=list, description="summary에서 인용한 근거 ID")
    limitations: list[str] = Field(default_factory=list, description="누락·미검증 내용")
    round: int = 1


class SupervisorDecision(BaseModel):
    """조사 후 supervisor 검토 결과 (2라운드 재분배 여부)."""

    complete: bool = Field(description="모든 하위 주제가 충분히 조사됐으면 True")
    retry_subtopic_ids: list[str] = Field(default_factory=list, description="다시 조사할 하위 주제 id")
    reason: str = ""


class OutlineSection(BaseModel):
    """보고서 섹션 하나와 그 섹션에서 쓸 근거."""

    title: str
    subtopic_ids: list[str]
    evidence_ids: list[str]


class Outline(BaseModel):
    sections: list[OutlineSection] = Field(min_length=1)


class ReviewIssue(BaseModel):
    severity: Literal["error", "warning"]
    location: str = Field(description="문제가 있는 섹션 제목 또는 문장")
    message: str


class Review(BaseModel):
    """reviewer 출력: 수치 정합성·섹션 간 모순·출처 누락 검사."""

    passed: bool
    issues: list[ReviewIssue] = Field(default_factory=list)


# ─────────────────────────────────────────────
# 그래프 state (TypedDict)
# ─────────────────────────────────────────────

class ResearchInput(TypedDict):
    """그래프 입력 (API /research 요청)."""

    request: str                      # 예: "삼성전자 최근 3년 재무 분석 보고서"


class ResearchState(TypedDict, total=False):
    """전체 그래프 state. total=False: 노드들이 순서대로 채워 간다."""

    # 입력
    request: str

    # clarify → brief
    clarify: ClarifyResult
    brief: Brief

    # supervisor ⇄ researchers
    round: int                                                  # 현재 조사 라운드 (1부터)
    findings: Annotated[list[Finding], override_reducer]        # 병렬 결과 모음 / 초기화 가능
    supervisor_decision: SupervisorDecision

    # 작성 → 검토
    outline: Outline
    report: str                                                 # 1차: writer가 한 번에 작성
    review: Review

    # 진행 상황 (SSE)·오류
    step: str                                                   # 예: "brief", "research:round1"
    errors: Annotated[list[str], operator.add]


class ResearchOutput(TypedDict, total=False):
    """그래프 출력 (Run Store 저장·API 응답)."""

    report: str
    review: Review
    findings: list[Finding]
    brief: Brief


class ResearcherInput(TypedDict):
    """supervisor가 Send로 보내는 researcher 입력. ← 루프 담당과의 계약 (입력)"""

    subtopic: SubTopic
    brief: Brief
    round: int


class ResearcherOutput(TypedDict):
    """researcher가 부모 그래프로 돌려주는 것. ← 루프 담당과의 계약 (출력)"""

    findings: list[Finding]           # 보통 1개, override_reducer로 부모에 합쳐짐


# ─────────────────────────────────────────────
# 자체 테스트: mock researcher 병렬 실행 + 초기화
# ─────────────────────────────────────────────

if __name__ == "__main__":
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Send

    def fan_out(state: ResearchState) -> list[Send]:
        brief = state["brief"]
        return [
            Send("researcher", ResearcherInput(subtopic=t, brief=brief, round=1))
            for t in brief.subtopics
        ]

    def researcher(inp: ResearcherInput) -> ResearcherOutput:   # mock 루프
        t = inp["subtopic"]
        return {"findings": [Finding(
            subtopic_id=t.id, status="draft",
            summary=f"{t.title} 조사 결과 [S1]", evidence_ids=["S1"], round=inp["round"],
        )]}

    g = StateGraph(ResearchState)
    g.add_node("researcher", researcher)
    g.add_conditional_edges(START, fan_out, ["researcher"])
    g.add_edge("researcher", END)
    app = g.compile()

    brief = Brief(
        company="삼성전자", years=[2023, 2024, 2025], objective="재무 건전성 분석",
        subtopics=[
            SubTopic(id="profitability", title="수익성", questions=["영업이익률 추이는?"]),
            SubTopic(id="stability", title="재무 안정성", questions=["부채비율은?"]),
            SubTopic(id="risk", title="리스크", questions=["주요 소송·우발부채는?"]),
        ],
    )
    out = app.invoke({"brief": brief, "findings": []})
    print("병렬 결과 수:", len(out["findings"]))                       # 3
    print([f.subtopic_id for f in out["findings"]])

    # override_reducer 단독 확인
    print(override_reducer([1, 2], [3]))                                # [1, 2, 3]
    print(override_reducer([1, 2], {"type": "override", "value": []}))  # []