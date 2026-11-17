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
    if not set(result.evidence_ids).issubset(available):
        raise ValueError("근거 평가에 검색되지 않은 출처 ID가 포함되어 있습니다.")
    if result.sufficient and (not result.evidence_ids or result.missing_items):
        raise ValueError("근거 충분 판정에 유효한 출처가 없거나 미해결 항목이 남아 있습니다.")
    return result
