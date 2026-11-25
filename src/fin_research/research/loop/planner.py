"""질문 하나를 숫자·계산·설명 조사 요구로 구조화한다."""

from pathlib import Path
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field, model_validator

from fin_research.tools.financial_models import FinancialContext, Metric, RatioName
from fin_research.tools.financial_catalog import capabilities, CATALOG


class ResearchPlan(BaseModel):
    """모델은 요구사항만 선택하며 실행 코드·SQL·조회 기준을 임의로 만들지 않는다."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["ready", "needs_clarification"]
    metrics: list[Metric] = Field(max_length=len(CATALOG))
    ratios: list[RatioName] = Field(max_length=4)
    document_question: str | None
    reason: str = Field(min_length=1)
    unsupported_items: list[str] = Field(default_factory=list, description="현재 처리할 수 없는 요구만 별도 기록. 지원 항목은 계속 실행")

    @model_validator(mode="after")
    def check_actions(self):
        """빈 계획·중복 요구·확인 요청과 실행 요구의 혼합을 거부한다."""
        if len(set(self.metrics)) != len(self.metrics) or len(set(self.ratios)) != len(self.ratios):
            raise ValueError("조사 지표나 계산이 중복되었습니다.")
        if self.document_question is not None and not self.document_question.strip():
            raise ValueError("설명 검색 질문은 비어 있을 수 없습니다.")
        has_actions = bool(self.metrics or self.ratios or self.document_question)
        if self.status == "ready" and not has_actions:
            raise ValueError("실행할 조사가 없습니다.")
        if self.status == "needs_clarification" and has_actions:
            if not self.unsupported_items:
                raise ValueError("확인이 필요한 상태에서는 실행 과제를 비워야 합니다.")
            # 지원 항목을 선택했고 미지원 요구도 명시한 혼합 계획은 부분 실행한다.
            self.status = "ready"
        return self


def create_plan(question: str, context: FinancialContext, model) -> ResearchPlan:
    """고정된 기업·연간 기준을 모델에 전달하고 구조화된 조사 계획을 받는다."""
    if not question.strip() or len(question) > 10000:
        raise ValueError("질문은 비어 있지 않은 10,000자 이내 문자열이어야 합니다.")
    path = Path(__file__).resolve().parents[4] / "configs/prompts/research/loop_planner.md"
    prompt = ChatPromptTemplate.from_messages([
        ("system", path.read_text(encoding="utf-8")),
        ("human", "지원 기능\n{capabilities}\n고정 조사 기준\n{context}\n\n질문\n{question}"),
    ])
    chain = prompt | model.with_structured_output(ResearchPlan, method="json_schema", strict=True)
    return ResearchPlan.model_validate(chain.invoke({"context": context.model_dump_json(), "question": question,
                                                     "capabilities": capabilities()}))
