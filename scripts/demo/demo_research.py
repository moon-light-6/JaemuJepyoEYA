"""clarify → brief → researcher 병렬 조사 → supervisor 검토(최대 2라운드) 시연.

실행 (프로젝트 루트, .env에 OPENAI_API_KEY, data/chroma_multi에 해당 기업 색인 필요):
    python scripts/demo/demo_research.py "삼성전자 수익성 위주로 분석해줘"
    python scripts/demo/demo_research.py "삼성전자 재무 분석" --mock          # 검색·루프 없이 흐름만
    python scripts/demo/demo_research.py "삼성전자 재무 분석" --questions 1   # 하위 주제당 질문 1개 (비용 절약)
"""

from __future__ import annotations

import argparse
import asyncio
import time
import sys
from pathlib import Path

# 패키지를 별도 설치하지 않아도 프로젝트 루트에서 실행할 수 있도록 한다.
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # scripts/demo/ → 프로젝트 루트
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.evidence.registry import EvidenceRegistry
from fin_research.llm.budget import Budget, BudgetExceeded
from fin_research.research.graph import build_graph, run_config
from fin_research.research.nodes.researcher import make_loop_researcher, mock_researcher


TITLE_CHARS = 30  # 주석 청크는 제목에 문단 전체가 들어 있는 경우가 있어 앞부분만 보여 준다


def short_title(title: str | None, limit: int = TITLE_CHARS) -> str:
    """'32. 우발채무 및 약정사항가. 당기말 현재 …(수백 자)' → '32. 우발채무 및 약정사항가. 당기말 …'"""
    title = " ".join((title or "").split())
    return title if len(title) <= limit else title[:limit].rstrip() + "…"


def describe_location(source) -> str:
    """EvidenceRef·Evidence 공통: 'PDF 84쪽' 또는 '연결 손익계산서 · 파일 · XPath'."""
    if source.pdf_page:
        return f"{source.source_file or ''} PDF {source.pdf_page}쪽".strip()
    if source.source_xpath:
        title = f"{short_title(source.title)} · " if source.title else ""
        return f"{title}{source.source_file} {source.source_xpath}"
    return "위치 정보 없음"


def quotes_by_id(state: dict) -> dict[str, str]:
    """findings의 출처에 붙은 짧은 근거 조각 (S# → 조각). 같은 출처는 처음 것."""
    quotes: dict[str, str] = {}
    for finding in state.get("findings", []):
        for source in finding.sources:
            if getattr(source, "quote", None):
                quotes.setdefault(source.id, source.quote)
    return quotes


def one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def print_sources(registry: EvidenceRegistry, quotes: dict[str, str] | None = None, preview: int = 160) -> None:
    """보고서 전체 출처 목록 (장부 번호 순). 짧은 근거 조각이 있으면 그것을, 없으면 원문 앞부분을 보여 준다."""
    evidences = registry.values()
    if not evidences:
        print("\n[출처] 장부에 등록된 근거가 없습니다 (--mock은 임시 번호만 씁니다).")
        return
    quotes = quotes or {}
    print(f"\n[출처] 근거 {len(evidences)}개")
    for e in evidences:
        print(f"  {e.evidence_id:>4}  {describe_location(e)}")
        print(f"        \"{one_line(quotes.get(e.evidence_id) or e.text, preview)}\"")


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
            print(f"   - {src.id}: {describe_location(src)}")
            if getattr(src, "quote", None):
                print(f"       └ \"{one_line(src.quote, 160)}\"")
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
    registry = EvidenceRegistry()  # 실행 하나에 장부 하나. 끝난 뒤 출처 목록을 읽으려고 직접 만든다
    started = time.monotonic()
    try:
        state = await build_graph(researcher).ainvoke(
            {"request": args.request, "findings": []}, run_config(budget, registry),
        )
    except BudgetExceeded as e:
        print(f"예산 초과로 중단: {e}")
        return 1
    print_result(state)
    print_sources(registry, quotes_by_id(state))

    s = budget.summary()
    by_role = ", ".join(f"{role} {u['calls']}" for role, u in s["by_role"].items())
    print(f"\n예산: {s['calls']}회 호출, 토큰 {s['total_tokens']:,}개, {time.monotonic() - started:.1f}초")
    print(f"      역할별: {by_role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
