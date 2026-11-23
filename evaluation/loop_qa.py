"""루프 평가 질문·정답·실행 관측값의 공통 자료형과 JSONL 로더."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Category = Literal[
    "numeric_lookup", "document_lookup", "sql_calculation",
    "mixed_lookup", "abstention", "research_control",
]
ToolName = Literal["sql", "doc_search", "calc"]
CATEGORY_LABELS = {
    "numeric_lookup": "숫자 조회", "document_lookup": "설명 검색",
    "sql_calculation": "SQL 후 계산", "mixed_lookup": "숫자와 설명",
    "abstention": "답변 보류", "research_control": "재검색과 종료",
}


class StrictModel(BaseModel):
    """오타 필드와 NaN·무한대가 평가 자료에 섞이지 않도록 제한한다."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class QAContext(StrictModel):
    """회사·실적 연도·연결/별도·통화의 비교 기준을 보관한다."""

    corp_code: str = Field(pattern=r"^\d{8}$")
    company: str = Field(min_length=1)
    year: int = Field(ge=1900, le=9999)
    scope: Literal["consolidated", "separate"]
    currency: str = "KRW"


class ExpectedNumber(StrictModel):
    """정답 숫자와 단위 및 절대 허용 오차를 정의한다."""

    metric: str = Field(min_length=1)
    value: Decimal
    unit: str = Field(min_length=1)
    tolerance: Decimal = Field(default=Decimal(0), ge=0)


class ExpectedResult(StrictModel):
    """정답·도구 순서·출처·반복 횟수·종료 사유를 독립적으로 정의한다."""

    status: Literal["answered", "abstained", "error"]
    numbers: list[ExpectedNumber] = Field(default_factory=list)
    required_phrases: list[str] = Field(default_factory=list)
    required_source_ids: list[str] = Field(default_factory=list)
    tools: list[ToolName]
    stop_reasons: list[str] = Field(min_length=1)
    min_rounds: int = Field(default=0, ge=0)
    max_rounds: int = Field(default=0, ge=0, le=5)

    @model_validator(mode="after")
    def check_expectations(self):
        """중복 지표와 모순되는 정답·반복 범위를 미리 거부한다."""
        if self.min_rounds > self.max_rounds:
            raise ValueError("최소 반복 횟수가 최대 반복 횟수보다 큽니다.")
        if len({number.metric for number in self.numbers}) != len(self.numbers):
            raise ValueError("정답 지표가 중복되었습니다.")
        if self.status != "answered" and self.numbers:
            raise ValueError("보류·오류 정답에 확정 숫자를 넣을 수 없습니다.")
        return self


class FixtureDocument(StrictModel):
    """모의 검색에 사용할 문서 원문과 고유 ID를 보관한다."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class FixtureDecision(StrictModel):
    """모의 LLM의 평가 응답을 지정하며 실제 모델의 판단 성능과 구분한다."""

    sufficient: bool
    source_ids: list[str] = Field(default_factory=list)
    reason: str
    next_query: str | None = None


class FixtureSpec(StrictModel):
    """정답을 보지 않는 모의 도구에 전달할 데이터와 시나리오만 정의한다."""

    task: Literal["numeric", "document", "margin", "mixed", "missing", "control"]
    amounts: dict[str, Decimal] = Field(default_factory=dict)
    fact_years: dict[str, int] = Field(default_factory=dict)
    db_error: bool = False
    documents: list[FixtureDocument] = Field(default_factory=list)
    retrieval_rounds: list[list[str]] = Field(default_factory=list)
    decisions: list[FixtureDecision] = Field(default_factory=list)
    max_rounds: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def check_fixture_sources(self):
        """문서 ID 중복과 시나리오의 존재하지 않는 문서 참조를 차단한다."""
        ids = {document.id for document in self.documents}
        if len(ids) != len(self.documents):
            raise ValueError("모의 문서 ID가 중복되었습니다.")
        used = {source for batch in self.retrieval_rounds for source in batch}
        used.update(source for decision in self.decisions for source in decision.source_ids)
        if used - ids:
            raise ValueError(f"모의 문서가 없는 ID입니다: {sorted(used - ids)}")
        return self


class LoopQACase(StrictModel):
    """한 평가 질문과 정답 계약, 별도 모의 입력을 묶는다."""

    id: str = Field(min_length=1)
    category: Category
    question: str = Field(min_length=1)
    context: QAContext
    data_origin: Literal["synthetic_fixture", "verified_source"]
    source_note: str = Field(min_length=1)
    expected: ExpectedResult
    fixture: FixtureSpec | None = None


class NumericAnswer(StrictModel):
    """실행 결과의 숫자·단위·비교 기준·입력 출처를 함께 보관한다."""

    metric: str
    value: Decimal
    unit: str
    context: QAContext
    source_ids: list[str] = Field(min_length=1)


class QAEvidence(StrictModel):
    """실행 중 실제로 확보한 근거와 해당 자료의 비교 기준을 기록한다."""

    id: str
    text: str
    source_file: str
    context: QAContext


class ToolEvent(StrictModel):
    """도구 실행 또는 차단 사실을 순서대로 기록한다."""

    tool: ToolName
    status: Literal["ok", "empty", "error", "blocked"]
    detail: str


class LoopObservation(StrictModel):
    """채점할 시스템 출력의 공통 형식으로, 정답 정보는 포함하지 않는다."""

    case_id: str
    status: Literal["answered", "abstained", "error"]
    answer: str
    numbers: list[NumericAnswer] = Field(default_factory=list)
    evidences: list[QAEvidence] = Field(default_factory=list)
    cited_source_ids: list[str] = Field(default_factory=list)
    tool_trace: list[ToolEvent] = Field(default_factory=list)
    stop_reason: str
    rounds: int = Field(default=0, ge=0)
    limitations: list[str] = Field(default_factory=list)


def load_jsonl(path: str | Path, schema: type[StrictModel]) -> list:
    """JSONL을 자료형에 맞게 읽고 행 번호와 함께 형식 오류를 설명한다."""
    records = []
    ids = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = schema.model_validate(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
        identifier = record.id if isinstance(record, LoopQACase) else record.case_id
        if identifier in ids:
            raise ValueError(f"중복된 평가 ID입니다: {identifier}")
        ids.add(identifier)
        records.append(record)
    if not records:
        raise ValueError(f"평가 파일이 비어 있습니다: {path}")
    return records
