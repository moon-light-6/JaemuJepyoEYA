"""검증 그래프: claim_extract → verify_claim ×N (Send 병렬) → aggregate.

    START → claim_extract ─(Send ×주장 수)─▶ verify_claim ─▶ aggregate → END
                 └──────────(주장 0개·기업/PDF 오류)──────────▶ aggregate

- claim_extract가 Command(goto=[Send ...] 또는 "aggregate")로 행선지를 정한다.
- 고정 엣지는 START → claim_extract, verify_claim → aggregate, aggregate → END.
  병렬 verify_claim이 모두 끝난 뒤 aggregate는 한 번만 실행된다.
- Budget·AccountStore는 state가 아니라 config["configurable"]로 넘긴다 (직렬화 불가·캐시 공유).
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from fin_research.llm.budget import Budget, load_budget_config
from fin_research.verify.accounts import AccountStore
from fin_research.verify.nodes.aggregate import aggregate_node
from fin_research.verify.nodes.claim_extract import claim_extract_node
from fin_research.verify.nodes.verify_claim import verify_claim_node
from fin_research.verify.state import VerifyInput, VerifyOutput, VerifyState


def build_graph():
    g = StateGraph(VerifyState, input_schema=VerifyInput, output_schema=VerifyOutput)
    g.add_node("claim_extract", claim_extract_node)
    g.add_node("verify_claim", verify_claim_node)
    g.add_node("aggregate", aggregate_node)

    g.add_edge(START, "claim_extract")
    g.add_edge("verify_claim", "aggregate")
    g.add_edge("aggregate", END)
    return g.compile()


def run_config(budget: Budget | None = None, account_store: AccountStore | None = None, **configurable) -> dict:
    """실행 설정: 동시 verify_claim 수(budget.yaml verify)와 노드가 꺼내 쓸 budget·account_store."""
    verify = load_budget_config().get("verify") or {}
    return {
        "max_concurrency": verify.get("max_concurrent_verify") or 8,
        "configurable": {"budget": budget, "account_store": account_store or AccountStore(), **configurable},
    }
