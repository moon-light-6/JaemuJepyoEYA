"""허용된 재무비율 함수만 실행하며 숫자의 기준과 출처를 검사한다."""

from fin_research.tools import ratios
from fin_research.tools.financial_models import (
    CalculationResult, FinancialContext, FinancialDataError, FinancialFact, RatioName,
)


RATIO_INPUTS = {
    "debt_ratio": ("total_liabilities", "total_equity"),
    "current_ratio": ("current_assets", "current_liabilities"),
    "operating_margin": ("operating_profit", "revenue"),
    "roe": ("net_income", "total_equity"),
}


def calculate_ratio(name: RatioName, facts: list[FinancialFact], context: FinancialContext) -> CalculationResult:
    """동일 기준의 원 단위 입력만 사용해 비율을 계산한다. 임의 코드는 실행하지 않는다."""
    if name not in RATIO_INPUTS:
        raise ValueError(f"지원하지 않는 계산입니다: {name}")
    selected = {}
    for metric in RATIO_INPUTS[name]:
        years = [context.year - 1, context.year] if name == "roe" and metric == "total_equity" else [context.year]
        for year in years:
            candidates = [fact for fact in facts if fact.metric == metric and fact.context.year == year]
            if len(candidates) != 1:
                raise FinancialDataError(f"{year}년 {metric} 입력을 하나로 확정하지 못했습니다.")
            fact = candidates[0]
            if fact.context != context.model_copy(update={"year": year}) or fact.unit != "원":
                raise FinancialDataError("계산 입력의 회사·기간·기준·통화·단위가 다릅니다.", "context_mismatch")
            selected[f"{metric}:{year}"] = fact
    if name == "roe":
        args = [selected[f"net_income:{context.year}"].value,
                selected[f"total_equity:{context.year - 1}"].value,
                selected[f"total_equity:{context.year}"].value]
        function = ratios.return_on_equity
    else:
        args = [selected[f"{metric}:{context.year}"].value for metric in RATIO_INPUTS[name]]
        function = {"debt_ratio": ratios.debt_ratio, "current_ratio": ratios.current_ratio,
                    "operating_margin": ratios.operating_profit_margin}[name]
    value = function(*args)
    return CalculationResult(metric=name, value=value, context=context,
                             source_ids=[fact.id for fact in selected.values()],
                             inputs={key: fact.value for key, fact in selected.items()})
