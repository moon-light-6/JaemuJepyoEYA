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
    return normalize_citations(result, available)


_CITATION = re.compile(r"\[([^\[\]]*?\bS\d+\b[^\[\]]*?)\]")  # [S1], [S1, S3], [S1·S3]
_SOURCE_ID = re.compile(r"\bS\d+\b")


def inline_citations(answer: str) -> list[str]:
    """답변 본문의 대괄호 안 출처 번호 (등장 순서, 중복 제거). '갤럭시 S25' 같은 본문 단어는 제외."""
    ids = [i for group in _CITATION.findall(answer) for i in _SOURCE_ID.findall(group)]
    return list(dict.fromkeys(ids))


def normalize_citations(result: GroundedAnswer, available: set[str]) -> GroundedAnswer:
    """출처 표기를 검사하되, 형식만 어긋난 맞는 답은 버리지 않고 바로잡는다.

    오류로 처리 (findings_node가 한 번 다시 요청):
      - 검색 결과에 없는 출처 ID를 쓴 경우 (지어낸 인용)
      - draft인데 출처가 하나도 없는 경우
    바로잡음:
      - 본문에 [S#]가 없고 evidence_ids만 있으면 → 답변 끝에 [S1, S2]를 붙인다
      - 둘 다 있는데 목록이 다르면 → 본문 표기를 기준으로 evidence_ids를 맞춘다
    """
    listed = list(dict.fromkeys(result.evidence_ids))
    inline = inline_citations(result.answer)
    unknown = sorted({*listed, *inline} - available)
    if unknown:
        raise ValueError(f"모델이 검색 결과에 없는 출처 ID를 인용했습니다: {', '.join(unknown)}")
    if result.status == "draft" and not listed and not inline:
        raise ValueError("답변 초안에 출처가 없습니다.")
    if listed and not inline:
        return result.model_copy(update={"answer": f"{result.answer.rstrip()} [{', '.join(listed)}]"})
    if inline != listed:
        return result.model_copy(update={"evidence_ids": inline})
    return result
