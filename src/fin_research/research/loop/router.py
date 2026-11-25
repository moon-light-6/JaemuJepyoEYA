"""조사 계획을 허용된 도구 순서와 필요한 입력 지표로 변환한다."""

from dataclasses import dataclass

from fin_research.research.loop.planner import ResearchPlan
from fin_research.tools.calc_tool import RATIO_INPUTS


@dataclass(frozen=True)
class ToolRoute:
    """실행 순서와 SQL로 확보할 입력 지표를 보관한다."""

    steps: list[str]
    lookup_metrics: list[str]
    include_prior_equity: bool


def route_plan(plan: ResearchPlan) -> ToolRoute:
    """SQL → 계산 → 설명 검색 순서로 실행하며 계산 입력은 코드가 결정한다."""
    if plan.status != "ready":
        return ToolRoute([], [], False)
    metrics = list(plan.metrics)
    for name in plan.ratios:
        for metric in RATIO_INPUTS[name]:
            if metric not in metrics:
                metrics.append(metric)
    steps = (["sql"] if metrics else []) + (["calc"] if plan.ratios else [])
    if plan.document_question:
        steps.append("doc_search")
    return ToolRoute(steps, metrics, "roe" in plan.ratios)
