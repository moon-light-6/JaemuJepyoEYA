"""Deep Research 그래프 조립 (2주차: clarify → brief → researcher 병렬 → supervisor 검토).

    START → clarify ─▶ brief ─(fan_out)─▶ researcher ×N ─▶ supervisor_review ─(재조사)─▶ researcher
              └▶ END (되묻기)                                                └▶ END (3주차: compress)
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from fin_research.llm.budget import Budget, load_budget_config
from fin_research.research.nodes.brief import brief_node
from fin_research.research.nodes.clarify import clarify_node, route_after_clarify
from fin_research.research.nodes.researcher import make_loop_researcher
from fin_research.research.nodes.supervisor import fan_out, route_after_review, supervisor_review
from fin_research.research.state import ResearchState


def build_graph(researcher: Callable | None = None):
    """researcher를 바꿔 끼울 수 있다 (테스트: mock_researcher, 실제: make_loop_researcher())."""
    g = StateGraph(ResearchState)
    g.add_node("clarify", clarify_node)
    g.add_node("brief", brief_node)
    g.add_node("researcher", researcher or make_loop_researcher())
    g.add_node("supervisor_review", supervisor_review)

    g.add_edge(START, "clarify")
    g.add_conditional_edges("clarify", route_after_clarify, ["brief", END])
    g.add_conditional_edges("brief", fan_out, ["researcher"])
    g.add_edge("researcher", "supervisor_review")
    g.add_conditional_edges("supervisor_review", route_after_review, ["researcher", END])
    return g.compile()


def run_config(budget: Budget | None = None, **configurable) -> dict:
    """실행 설정: 동시 researcher 수(budget.yaml)와 노드가 꺼내 쓸 budget 등."""
    research = load_budget_config().get("research") or {}
    return {
        "max_concurrency": research.get("max_concurrent_researchers") or 3,
        "configurable": {"budget": budget, **configurable},
    }
