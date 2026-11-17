"""clarify → brief 시연: 요청 문장을 넣으면 되묻기 또는 하위 주제별 조사 계획을 출력한다.

실행 (프로젝트 루트, .env에 OPENAI_API_KEY 필요):
    python scripts/demo_brief.py
    python scripts/demo_brief.py "삼성전자 수익성 위주로 분석해줘"
    python scripts/demo_brief.py "SK 재무 분석해줘" "에코프로비엠 최근 3년 현금흐름 위주로 분석해줘"
"""

from __future__ import annotations

import argparse
import asyncio

from fin_research.llm.budget import Budget, BudgetExceeded
from fin_research.research.nodes.brief import brief_node
from fin_research.research.nodes.clarify import clarify_node, route_after_clarify

DEFAULT_REQUESTS = [
    "SK 재무 분석해줘",
    "에코프로비엠 최근 3년 현금흐름 위주로 분석해줘",
]


async def run(request: str, config: dict) -> None:
    state: dict = {"request": request}
    print(f"\n▶ {request}")

    state.update(await clarify_node(state, config))
    clarify = state["clarify"]
    if route_after_clarify(state) != "brief":
        print(f"  되묻기: {clarify.question}")
        return
    print(f"  clarify: {clarify.company} {clarify.years} focus={clarify.focus}")
    for note in clarify.notes:
        print(f"           메모: {note}")

    state.update(await brief_node(state, config))
    brief = state["brief"]
    print(f"  목적: {brief.objective}")
    for subtopic in brief.subtopics:
        print(f"  [{subtopic.title}] 지표: {', '.join(subtopic.metrics)}")
        for question in subtopic.questions:
            print(f"     - {question}")
    for error in state.get("errors", []):
        print(f"  ⚠ {error}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="clarify → brief 시연")
    parser.add_argument("requests", nargs="*", help="분석 요청 문장 (생략하면 예시 2개)")
    args = parser.parse_args()

    budget = Budget.from_config()
    config = {"configurable": {"budget": budget}}
    try:
        for request in args.requests or DEFAULT_REQUESTS:
            await run(request, config)
    except BudgetExceeded as e:
        print(f"\n예산 초과로 중단: {e}")

    s = budget.summary()
    by_role = ", ".join(f"{role} {u['calls']}회" for role, u in s["by_role"].items())
    print(
        f"\n예산: {s['calls']}회 호출, 토큰 {s['total_tokens']:,}개, "
        f"{s['elapsed_sec']}초 ({by_role})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
