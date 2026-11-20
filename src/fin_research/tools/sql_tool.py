"""스키마를 전달해 PostgreSQL SELECT를 생성하고 읽기 전용으로 실행한다."""

from pathlib import Path
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp, parse_one

from fin_research.progress import ProgressCallback, report_progress
from fin_research.storage.sql_store import (
    PostgresReadOnlyStore,
    UnsafeSQLError,
    schema_description,
    validate_readonly_sql,
)


PROMPT_PATH = Path(__file__).resolve().parents[3] / "configs/prompts/research/sql.md"


class SQLPlan(BaseModel):
    """기준이 불명확한 질문은 SQL을 실행하지 않고 확인을 요청한다."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["ready", "needs_clarification"]
    sql: str | None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_status(self):
        if self.status == "ready":
            if not self.sql or not self.sql.strip():
                raise ValueError("ready 상태에는 SQL이 필요합니다.")
        elif self.sql is not None:
            raise ValueError("확인이 필요한 질문은 sql을 null로 반환해야 합니다.")
        return self


def generate_sql(question: str, model, *, progress: ProgressCallback | None = None) -> SQLPlan:
    """구조화된 SQL 초안을 생성한 뒤 실행 전 구문 검사를 수행한다."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("질문을 입력하세요.")
    if len(question) > 10000:
        raise ValueError("질문은 10,000자 이내로 입력하세요.")
    prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPT_PATH.read_text(encoding="utf-8")),
        ("human", "조회 요청\n{question}"),
    ])
    chain = prompt | model.with_structured_output(SQLPlan, method="json_schema", strict=True)
    request = question.strip()
    for attempt in range(2):
        report_progress(progress, f"스키마를 바탕으로 SQL 생성 중 · 시도 {attempt + 1}/2")
        plan = SQLPlan.model_validate(chain.invoke({
            "question": request, "schema": schema_description(),
        }))
        if plan.status == "needs_clarification":
            report_progress(progress, f"추가 확인 필요 · DB 조회 생략 · {plan.reason}")
            return plan
        try:
            report_progress(progress, "생성 SQL 검사 중 · 허용 뷰·읽기 전용 구문·기간 조건 확인")
            normalized = validate_readonly_sql(plan.sql)
            # 보고서 연도만 필터하면 전기·전전기 수치까지 섞이는 오류를 차단한다.
            tree = parse_one(normalized, read="postgres")
            for select in tree.find_all(exp.Select):
                where = select.args.get("where")
                columns = {column.name for column in where.find_all(exp.Column)} if where else set()
                if "fiscal_year" in columns and not columns.intersection(
                    {"period_start", "period_end", "period_role"}
                ):
                    raise UnsafeSQLError(
                        "fiscal_year만으로 실적 기간을 선택할 수 없습니다. "
                        "요청한 실적 기간에 맞는 period_end 또는 period_role 조건을 추가하세요."
                    )
            report_progress(progress, f"SQL 검사 통과 · {plan.reason}")
            report_progress(progress, f"실행할 SQL: {normalized}")
            return plan.model_copy(update={"sql": normalized})
        except UnsafeSQLError as exc:
            report_progress(progress, f"SQL 검사 실패 · {exc}")
            if attempt:
                raise
            report_progress(progress, "검사 결과를 반영해 SQL 수정을 요청합니다.")
            request = question.strip() + "\n이전 SQL의 실행 전 검사가 실패했습니다: " + str(exc)
    raise RuntimeError("SQL 생성에 실패했습니다.")


class FinancialSQLTool:
    """모델과 DB 실행기를 주입하여 테스트와 실제 실행을 같은 인터페이스로 제공한다."""

    def __init__(self, model, store: PostgresReadOnlyStore, *,
                 progress: ProgressCallback | None = None):
        self.model = model
        self.store = store
        self.progress = progress

    def run(self, question: str) -> dict:
        report_progress(self.progress, f"재무 SQL 조사 시작 · 질문: {question}")
        plan = generate_sql(question, self.model, progress=self.progress)
        if plan.status == "needs_clarification":
            return {"question": question, "plan": plan.model_dump(), "result": None}
        report_progress(self.progress, "PostgreSQL 읽기 전용 조회 중")
        result = self.store.execute(plan.sql)
        report_progress(self.progress, f"조회 완료 · {result.row_count}행 · "
                        f"행 수 제한으로 잘림: {'예' if result.truncated else '아니오'}")
        return {"question": question, "plan": plan.model_dump(), "result": result.to_dict()}
