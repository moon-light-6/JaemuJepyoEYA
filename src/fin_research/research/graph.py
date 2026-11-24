"""Deep Research 그래프 조립 (2주차: clarify → brief → researcher 병렬 → supervisor 검토).

    START → clarify ─▶ brief ─(Send ×N)─▶ researcher ─▶ supervisor_review ─(Send 재조사)─▶ researcher
              └▶ END (되묻기)                                            └▶ END (3주차: compress)

라우팅은 조건부 엣지 대신 각 노드가 Command(update=..., goto=...)로 직접 정한다.
- clarify:           goto "brief" 또는 END
- brief:             goto [Send("researcher", ...) × 하위 주제]
- supervisor_review: goto [Send("researcher", ...) × 재조사 대상] 또는 END
행선지는 노드 반환 타입 Command[Literal[...]]에서 LangGraph가 읽어 그래프 구조에 반영한다.
고정 엣지는 START → clarify, researcher → supervisor_review 둘뿐이다.
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import START, StateGraph

from fin_research.evidence.registry import EvidenceRegistry
from fin_research.llm.budget import Budget, load_budget_config
from fin_research.research.nodes.brief import brief_node
from fin_research.research.nodes.clarify import clarify_node
from fin_research.research.nodes.researcher import make_loop_researcher
from fin_research.research.nodes.supervisor import supervisor_review
from fin_research.research.state import ResearchState


def build_graph(researcher: Callable | None = None):
    """researcher를 바꿔 끼울 수 있다 (테스트: mock_researcher, 실제: make_loop_researcher())."""
    g = StateGraph(ResearchState)
    g.add_node("clarify", clarify_node)
    g.add_node("brief", brief_node)
    g.add_node("researcher", researcher or make_loop_researcher())
    g.add_node("supervisor_review", supervisor_review)

    g.add_edge(START, "clarify")
    g.add_edge("researcher", "supervisor_review")
    return g.compile()


def run_config(
    budget: Budget | None = None,
    evidence_registry: EvidenceRegistry | None = None,
    **configurable,
) -> dict:
    """실행 설정: 동시 researcher 수(budget.yaml)와 노드가 꺼내 쓸 budget·장부 등.

    evidence_registry를 주지 않으면 실행마다 새 장부를 만든다 (재사용하면 이전 실행의 번호가 이어진다).
    실행이 끝난 뒤 출처 목록이 필요하면 호출하는 쪽에서 장부를 만들어 넘기고, 그 장부를 읽는다.
    """
    research = load_budget_config().get("research") or {}
    return {
        "max_concurrency": research.get("max_concurrent_researchers") or 3,
        "configurable": {
            "budget": budget,
            "evidence_registry": evidence_registry if evidence_registry is not None else EvidenceRegistry(),
            **configurable,
        },
    }
