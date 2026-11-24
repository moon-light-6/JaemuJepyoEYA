"""질문과 누적 근거를 비교해 답변 가능 여부와 다음 검색어를 평가합니다."""

import json
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field

PROMPT_PATH = (
    Path(__file__).resolve().parents[4]
    / "configs/prompts/research/loop_evaluator.md"
)


class EvidenceEvaluation(BaseModel):
    """근거 충분성에 대한 모델 평가이며 공식 수치 검증과 구분합니다."""

    model_config = ConfigDict(extra="forbid")
    sufficient: bool
    reason: str = Field(min_length=1, description="평가 이유")
    evidence_ids: list[str] = Field(description="질문에 직접 관련된 제공 근거 ID")
    missing_items: list[str] = Field(description="답변을 위해 추가로 필요한 정보")
    next_query: str | None = Field(description="근거 부족 시 사용할 새로운 검색어. 종료 시 null")


def evaluate_evidence(
    question: str, evidences: list[dict], previous_queries: list[str], model,
) -> EvidenceEvaluation:
    prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPT_PATH.read_text(encoding="utf-8")),
        ("human", "원래 질문\n{question}\n\n이전 검색어\n{queries}\n\n누적 근거\n{evidences}"),
    ])
    chain = prompt | model.with_structured_output(
        EvidenceEvaluation, method="json_schema", strict=True,
    )
    result = EvidenceEvaluation.model_validate(chain.invoke({
        "question": question,
        "queries": json.dumps(previous_queries, ensure_ascii=False),
        "evidences": json.dumps(evidences, ensure_ascii=False),
    }))
    available = {item["evidence_id"] for item in evidences}
    return normalize_evaluation(result, available)


def normalize_evaluation(result: EvidenceEvaluation, available: set[str]) -> EvidenceEvaluation:
    """모델 평가의 모순을 예외 대신 보정한다. 예외를 던지면 이미 찾은 근거까지 버리고 질문 전체가 실패한다.

    - 검색되지 않은 출처 ID는 빼고, reason에 뺀 ID를 남긴다
    - '충분'인데 쓸 출처가 하나도 없으면 '부족'으로 바꾼다 (다음 검색어가 있으면 재검색)
    - '충분'인데 missing_items가 남아 있으면 '충분'을 유지한다 (부분 답변).
      찾은 근거로 답하고, 남은 항목은 findings_node가 limitations에 붙인다
    """
    known = [evidence_id for evidence_id in result.evidence_ids if evidence_id in available]
    dropped = [evidence_id for evidence_id in result.evidence_ids if evidence_id not in available]
    updates: dict = {}
    if dropped:
        updates["evidence_ids"] = known
        updates["reason"] = f"{result.reason} (검색되지 않은 출처 {', '.join(dropped)} 제외)"
    if result.sufficient and not known:
        updates["sufficient"] = False
        updates["missing_items"] = result.missing_items or ["답변에 사용할 출처가 지정되지 않았습니다."]
    return result.model_copy(update=updates) if updates else result
