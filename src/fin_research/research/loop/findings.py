"""검색 근거를 바탕으로 답변 초안을 생성하고 출처 표기의 유효성을 검사합니다."""

import json
import re
from pathlib import Path
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field

PROMPT_PATH = (
    Path(__file__).resolve().parents[4]
    / "configs/prompts/research/loop_findings.md"
)


class GroundedAnswer(BaseModel):
    """검색 근거로 작성한 답변 초안이며, 수치 검증 수행 여부는 별도 결과로 관리합니다."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["draft", "insufficient_evidence"]
    answer: str = Field(min_length=1, description="한국어 답변. 근거를 사용한 문장에 [S1]처럼 표기")
    evidence_ids: list[str] = Field(description="실제로 인용한 근거 ID 목록")
    limitations: list[str] = Field(description="누락된 정보나 검증하지 못한 내용")


def generate_findings(question: str, evidences: list[dict], model) -> GroundedAnswer:
    if not evidences:
        return GroundedAnswer(
            status="insufficient_evidence",
            answer="검색된 근거가 없어 답변을 작성할 수 없습니다.",
            evidence_ids=[],
            limitations=["관련 공시 근거를 추가로 확보해야 합니다."],
        )

    prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPT_PATH.read_text(encoding="utf-8")),
        ("human", "질문\n{question}\n\n검색 근거 JSON\n{evidences}"),
    ])
    structured_model = model.with_structured_output(
        GroundedAnswer, method="json_schema", strict=True,
    )
    chain = prompt | structured_model
    result = chain.invoke({
        "question": question,
        "evidences": json.dumps(evidences, ensure_ascii=False),
    })
    result = GroundedAnswer.model_validate(result)

    available = {item["evidence_id"] for item in evidences}
    listed = set(result.evidence_ids)
    inline = set(re.findall(r"\[(S\d+)\]", result.answer))
    if not listed.issubset(available) or not inline.issubset(available):
        raise ValueError("모델이 검색 결과에 없는 출처 ID를 인용했습니다.")
    if listed != inline:
        raise ValueError("답변의 출처 표기와 evidence_ids 목록이 일치하지 않습니다.")
    if result.status == "draft" and not listed:
        raise ValueError("답변 초안에 출처가 없습니다.")
    return result
