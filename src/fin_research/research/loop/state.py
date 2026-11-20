"""조사별 질문·근거·평가·종료 이유를 보관하는 내부 상태입니다."""

from typing import TypedDict


class LoopState(TypedDict):
    question: str
    query: str
    queries: list[str]
    evidences: list[dict]
    history: list[dict]
    evaluation: dict | None
    findings: dict | None
    rounds: int
    llm_calls: int
    stop_reason: str
    generation_errors: list[str]
