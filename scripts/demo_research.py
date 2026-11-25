"""clarify → brief → researcher 병렬 조사 → supervisor 검토(최대 2라운드) 시연.

실행 (프로젝트 루트, GPT·PostgreSQL 설정 필요. 설명 조사에만 XML/PDF 색인 필요):
    python scripts/demo_research.py "삼성전자 수익성 위주로 분석해줘"
    python scripts/demo_research.py "삼성전자 재무 분석" --mock          # 검색·루프 없이 흐름만
    python scripts/demo_research.py "삼성전자 재무 분석" --questions 1   # 하위 주제당 질문 1개 (비용 절약)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import sys
from pathlib import Path

# 패키지를 별도 설치하지 않아도 프로젝트 루트에서 실행할 수 있도록 한다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.llm.budget import Budget, BudgetExceeded
from fin_research.research.graph import build_graph, run_config
from fin_research.research.nodes.researcher import make_loop_researcher, mock_researcher
from fin_research.research.nodes.researcher import SearchToolCache
from fin_research.progress import TerminalProgress


def print_result(state: dict) -> None:
    clarify = state.get("clarify")
    if clarify and clarify.need_clarification:
        print(f"되묻기: {clarify.question}")
        return
    brief = state["brief"]
    print(f"기업: {brief.company} {brief.years} {brief.scope}  |  목적: {brief.objective}")
    for note in brief.notes:
        print(f"  메모: {note}")

    titles = {s.id: s.title for s in brief.subtopics}
    for f in sorted(state.get("findings", []), key=lambda f: list(titles).index(f.subtopic_id)):
        mark = "✅" if f.status == "draft" else "⚠️"
        print(f"\n{mark} [{titles[f.subtopic_id]}] (라운드 {f.round}, 상태 {f.status})")
        for line in f.summary.splitlines():
            print(f"   {line}")
        for src in f.sources:
            if src.source_xpath:
                where = f"{src.source_file} XPath {src.source_xpath}"
            elif src.pdf_page:
                where = f"{src.source_file} PDF {src.pdf_page}쪽"
            elif src.rcept_no:
                where = f"DB {src.source_file}, 공시 {src.rcept_no}, 계정 {src.account_name or src.account_id}"
            else:
                where = src.source_file or "위치 정보 없음"
            print(f"   - {src.id}: {where}")
        for note in f.limitations:
            print(f"   ! {note}")

    decision = state.get("supervisor_decision")
    if decision:
        print(f"\nsupervisor: {'완료' if decision.complete else '미완료'} — {decision.reason}")
    for error in state.get("errors", []):
        print(f"⚠ {error}")


async def main() -> int:
    """상위 조사에서 다중 도구 루프를 실행하고 질문별 확인 결과를 JSON으로 저장한다."""
    parser = argparse.ArgumentParser(description="Deep Research 2주차 흐름 시연")
    parser.add_argument("request", help="분석 요청 문장")
    parser.add_argument("--mock", action="store_true", help="researcher를 mock으로 (검색·루프 없음)")
    parser.add_argument("--questions", type=int, help="하위 주제당 질문 수 (기본: budget.yaml)")
    parser.add_argument("--source-format", choices=["xml", "pdf"], default="xml")
    parser.add_argument("--xml-version", type=int, choices=[1, 2], default=1)
    parser.add_argument("--persist-directory", type=Path)
    parser.add_argument("--rcept-no", help="조사할 공시 접수번호. 미지정 시 기업 설정 또는 단일 XML 처리 기록")
    parser.add_argument("--loop-rounds", type=int, choices=range(1, 6), default=None,
                        help="질문 하나의 문서 재검색 한도. 상위 Supervisor 재조사 라운드와 구분")
    parser.add_argument("--out", type=Path, help="조사 상태·질문별 숫자·출처·이력을 저장할 JSON 경로")
    parser.add_argument("--quiet", action="store_true", help="도구 실행 진행 로그를 숨긴다")
    args = parser.parse_args()

    tools = SearchToolCache(args.persist_directory) if args.persist_directory else None
    researcher = mock_researcher if args.mock else make_loop_researcher(
        questions_per_subtopic=args.questions, loop_rounds=args.loop_rounds, search_tools=tools,
        source_format=args.source_format, xml_version=args.xml_version,
        progress=None if args.quiet else TerminalProgress(),
    )
    budget = Budget.from_config()
    started = time.monotonic()
    try:
        state = await build_graph(researcher).ainvoke(
            {"request": args.request, "findings": []}, run_config(budget, research_rcept_no=args.rcept_no),
        )
    except BudgetExceeded as e:
        print(f"예산 초과로 중단: {e}")
        return 1
    print_result(state)
    if args.out:
        # 연구 상태에는 Budget·모델 객체나 청크 원문을 넣지 않고 검수 가능한 결과만 남긴다.
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(state, ensure_ascii=False, indent=2,
            default=lambda value: value.model_dump(mode="json")) + "\n", encoding="utf-8")
        print(f"조사 결과 JSON: {args.out}")

    s = budget.summary()
    by_role = ", ".join(f"{role} {u['calls']}" for role, u in s["by_role"].items())
    print(f"\n예산: {s['calls']}회 호출, 토큰 {s['total_tokens']:,}개, {time.monotonic() - started:.1f}초")
    print(f"      역할별: {by_role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
