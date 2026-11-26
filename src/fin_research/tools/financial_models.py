"""다중 도구 조사에서 공유하는 수치·기준·계산 결과 자료형."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# YAML 변경 후 재시작하면 구조화 출력에도 모든 지표가 반영된다.
from fin_research.tools.financial_catalog import CATALOG

Metric = Literal[tuple(CATALOG)]
RatioName = Literal["debt_ratio", "current_ratio", "roe", "operating_margin"]


class FinancialContext(BaseModel):
    """조회할 기업·연간 실적·연결/별도·공시·통화를 고정한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    company: str = Field(min_length=1)
    corp_code: str = Field(pattern=r"^\d{8}$")
    year: int = Field(ge=1900, le=9999)
    scope: Literal["consolidated", "separate"]
    rcept_no: str = Field(pattern=r"^\d{14}$")
    currency: Literal["KRW"] = "KRW"


class FinancialFact(BaseModel):
    """금액 하나와 실제 대상 연도·통화·공시 출처를 함께 보관한다."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)
    id: str = Field(min_length=1)
    metric: Metric
    value: Decimal
    unit: Literal["원"] = "원"
    context: FinancialContext
    account_name: str = ""
    account_id: str = ""
    statement_type: str = ""


class CalculationResult(BaseModel):
    """계산값과 이를 재현할 수 있는 입력 금액·출처를 보관한다."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    metric: RatioName
    value: Decimal
    unit: Literal["%"] = "%"
    context: FinancialContext
    source_ids: list[str]
    inputs: dict[str, Decimal]


class FinancialDataError(ValueError):
    """자료 부족·모호함·기준 불일치를 조사 종료 사유와 함께 전달한다."""

    def __init__(self, message: str, reason: str = "missing_data"):
        """사용자에게 보여 줄 설명과 기계가 처리할 종료 코드를 저장한다."""
        super().__init__(message)
        self.reason = reason
