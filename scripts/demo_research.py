"""clarify → brief → researcher 병렬 조사 → supervisor 검토(최대 2라운드) 시연.

실행 (프로젝트 루트, .env에 OPENAI_API_KEY, data/chroma_multi에 해당 기업 색인 필요):
    python scripts/demo_research.py "삼성전자 수익성 위주로 분석해줘"
    python scripts/demo_research.py "삼성전자 재무 분석" --mock          # 검색·루프 없이 흐름만
    python scripts/demo_research.py "삼성전자 재무 분석" --questions 1   # 하위 주제당 질문 1개 (비용 절약)
"""

from __future__ import annotations

import argparse
import asyncio
import time

from fin_research.llm.budget import Budget, BudgetExceeded
from fin_research.research.graph import build_graph, run_config
from fin_research.research.nodes.researcher import make_loop_researcher, mock_researcher


def print_result(state: dict) -> None:
    clarify = state.get("clarify")
    if clarify and clarify.need_clarification:
        print(f"되묻기: {clarify.question}")
        return
    brief = state["brief"]
    print(f"기업: {brief.company} {brief.years}  |  목적: {brief.objective}")
    for note in brief.notes:
        print(f"  메모: {note}")

    titles = {s.id: s.title for s in brief.subtopics}
    for f in sorted(state.get("findings", []), key=lambda f: list(titles).index(f.subtopic_id)):
        mark = "✅" if f.status == "draft" else "⚠️"
        print(f"\n{mark} [{titles[f.subtopic_id]}] (라운드 {f.round})")
        for line in f.summary.splitlines():
            print(f"   {line}")
        for src in f.sources:
            where = f"PDF {src.pdf_page}쪽" if src.pdf_page else "위치 정보 없음"
            print(f"   - {src.id}: {where}")
        for note in f.limitations:
            print(f"   ! {note}")

    decision = state.get("supervisor_decision")
    if decision:
        print(f"\nsupervisor: {'완료' if decision.complete else '미완료'} — {decision.reason}")
    for error in state.get("errors", []):
        print(f"⚠ {error}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Deep Research 2주차 흐름 시연")
    parser.add_argument("request", help="분석 요청 문장")
    parser.add_argument("--mock", action="store_true", help="researcher를 mock으로 (검색·루프 없음)")
    parser.add_argument("--questions", type=int, help="하위 주제당 질문 수 (기본: budget.yaml)")
    args = parser.parse_args()

    researcher = mock_researcher if args.mock else make_loop_researcher(questions_per_subtopic=args.questions)
    budget = Budget.from_config()
    started = time.monotonic()
    try:
        state = await build_graph(researcher).ainvoke(
            {"request": args.request, "findings": []}, run_config(budget),
        )
    except BudgetExceeded as e:
        print(f"예산 초과로 중단: {e}")
        return 1
    print_result(state)

    s = budget.summary()
    by_role = ", ".join(f"{role} {u['calls']}" for role, u in s["by_role"].items())
    print(f"\n예산: {s['calls']}회 호출, 토큰 {s['total_tokens']:,}개, {time.monotonic() - started:.1f}초")
    print(f"      역할별: {by_role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
