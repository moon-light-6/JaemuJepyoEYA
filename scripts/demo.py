"""터미널 데모: 1) 보고서 생성  2) 보고서 검증 (프로젝트 루트의 보고서.pdf만).

실행 (프로젝트 루트, .env에 OPENAI_API_KEY·DATABASE_URL 필요):
    python scripts/demo.py                 # 메뉴에서 고르기
    python scripts/demo.py --mode 1        # 바로 보고서 생성 (요청을 물어봄)
    python scripts/demo.py --mode 2        # 바로 보고서.pdf 검증 (기업명을 물어봄)
    python scripts/demo.py --mode 1 --mock # 검색·루프 없이 흐름만 (빠르고 저렴)

- 보고서 생성: 리서치 그래프(clarify → brief → researcher ×4 → supervisor)로 조사하고,
  주제별 결과와 출처(S#)를 Markdown 보고서로 저장한다 (생성보고서_{기업}.md).
  검색 색인이 있는 기업만 됩니다: KB금융, SK텔레콤, NAVER, 이마트(XML), 삼성전자(PDF).
- 보고서 검증: 프로젝트 루트의 보고서.pdf에서 숫자 주장을 뽑아 DART DB(Supabase)와 대조하고
  결과를 결과.json으로 저장한다. DB에 적재된 기업만 됩니다: KB금융, SK텔레콤, NAVER, 현대건설, 이마트.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

# 패키지를 별도 설치하지 않아도 프로젝트 루트에서 실행할 수 있도록 한다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import demo_research  # noqa: E402  (같은 폴더의 데모 출력 함수 재사용)
import demo_verify  # noqa: E402

from fin_research.evidence.registry import EvidenceRegistry  # noqa: E402
from fin_research.llm.budget import Budget, BudgetExceeded  # noqa: E402

REPORT_PDF = PROJECT_ROOT / "보고서.pdf"  # 검증은 이 파일만 받는다
RESULT_JSON = PROJECT_ROOT / "결과.json"
LINE = "─" * 60


# ─────────────────────────── 공통 ───────────────────────────

def ask(prompt: str, default: str | None = None) -> str:
    """입력이 비면 기본값. 입력이 끝나면(파이프·Ctrl+D) 기본값 또는 빈 문자열."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or (default or "")


def print_usage(budget: Budget, started: float) -> None:
    s = budget.summary()
    by_role = ", ".join(f"{role} {u['calls']}" for role, u in s["by_role"].items())
    print(f"\nLLM {s['calls']}회 · {s['total_tokens']:,}토큰 · {time.monotonic() - started:.1f}초")
    if by_role:
        print(f"  역할별: {by_role}")


# ─────────────────────────── 1. 보고서 생성 ───────────────────────────

def report_markdown(state: dict, registry: EvidenceRegistry) -> str:
    """리서치 결과 → Markdown 보고서. 본문 [S#]는 맨 아래 출처 목록과 번호가 같다."""
    brief = state["brief"]
    titles = {s.id: s.title for s in brief.subtopics}
    findings = sorted(state.get("findings", []), key=lambda f: list(titles).index(f.subtopic_id))
    years = ", ".join(str(y) for y in brief.years)
    lines = [
        f"# {brief.company} 재무 분석 보고서 ({years})",
        "",
        f"- 작성일: {date.today().isoformat()}",
        f"- 목적: {brief.objective}",
        "- 기준: DART 사업보고서 (연결)",
        "- 주의: AI가 공시 검색 결과로 작성한 초안입니다. 숫자는 `보고서 검증`으로 확인하세요.",
        "",
    ]
    for note in brief.notes:
        lines.append(f"> {note}")
    for f in findings:
        lines += ["", f"## {titles[f.subtopic_id]}", ""]
        if f.status == "draft":
            lines.append(f.summary)
        else:
            lines.append("_확보한 근거로 답할 수 없었습니다._")
        if f.limitations:
            lines += ["", "**한계**"] + [f"- {note}" for note in f.limitations]
    lines += ["", "## 출처", ""]
    evidences = registry.values()
    if not evidences:
        lines.append("_등록된 근거가 없습니다._")
    for e in evidences:
        lines.append(f"- **{e.evidence_id}** {demo_research.describe_location(e)}")
    return "\n".join(lines) + "\n"


async def run_generate(request: str, mock: bool, questions: int | None) -> int:
    from fin_research.research.graph import build_graph, run_config
    from fin_research.research.nodes.researcher import make_loop_researcher, mock_researcher

    researcher = mock_researcher if mock else make_loop_researcher(questions_per_subtopic=questions)
    budget = Budget.from_config()
    registry = EvidenceRegistry()
    started = time.monotonic()
    print(f"\n{LINE}\n보고서 생성 중… (기업당 1~2분, 처음에는 임베딩 모델을 불러와 더 걸립니다)\n{LINE}")
    try:
        state = await build_graph(researcher).ainvoke(
            {"request": request, "findings": []}, run_config(budget, registry),
        )
    except BudgetExceeded as e:
        print(f"예산 초과로 중단: {e}")
        return 1

    demo_research.print_result(state)
    if state.get("clarify") and state["clarify"].need_clarification:
        print_usage(budget, started)
        return 1
    demo_research.print_sources(registry)

    path = PROJECT_ROOT / f"생성보고서_{state['brief'].company}.md"
    path.write_text(report_markdown(state, registry), encoding="utf-8")
    print_usage(budget, started)
    print(f"\n보고서 저장: {path.relative_to(PROJECT_ROOT)}")
    return 0


# ─────────────────────────── 2. 보고서 검증 ───────────────────────────

async def run_verify(company: str) -> int:
    from fin_research.verify.graph import build_graph, run_config

    if not REPORT_PDF.is_file():
        print(f"검증할 파일이 없습니다: {REPORT_PDF.name}")
        print("프로젝트 루트(JaemuJepyoEYA/)에 보고서.pdf를 넣고 다시 실행하세요.")
        return 1
    budget = Budget.from_config()
    started = time.monotonic()
    print(f"\n{LINE}\n{REPORT_PDF.name} 검증 중… ({company})\n{LINE}")
    try:
        result = await build_graph().ainvoke(
            {"pdf_path": str(REPORT_PDF), "company": company}, run_config(budget),
        )
    except BudgetExceeded as e:
        print(f"예산 초과로 중단: {e}")
        return 1

    demo_verify.print_result(result)
    RESULT_JSON.write_text(json.dumps(demo_verify.to_json(result), ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print_usage(budget, started)
    print(f"\n결과 저장: {RESULT_JSON.name}")
    return 0


# ─────────────────────────── 메뉴 ───────────────────────────

MENU = f"""
{LINE}
 재무제표 Deep Research 데모
{LINE}
 1. 보고서 생성   (예: NAVER 2025년 재무 분석해줘)
 2. 보고서 검증   ({REPORT_PDF.name} → DART 공시 값과 대조)
 q. 종료
{LINE}"""


async def run_mode(mode: str, args: argparse.Namespace) -> int:
    if mode == "1":
        request = args.request or ask("분석 요청", "NAVER 2025년 재무 분석해줘")
        return await run_generate(request, args.mock, args.questions)
    if mode == "2":
        company = args.company or ask("보고서의 기업명", "NAVER")
        return await run_verify(company)
    print(f"알 수 없는 메뉴입니다: {mode}")
    return 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="보고서 생성·검증 터미널 데모")
    parser.add_argument("--mode", choices=("1", "2"), help="1: 보고서 생성, 2: 보고서.pdf 검증 (없으면 메뉴)")
    parser.add_argument("--request", help="보고서 생성 요청 문장 (mode 1)")
    parser.add_argument("--company", help="검증할 보고서의 기업명 (mode 2)")
    parser.add_argument("--mock", action="store_true", help="보고서 생성 시 검색·루프 없이 흐름만")
    parser.add_argument("--questions", type=int, default=1,
                        help="보고서 생성 시 하위 주제당 질문 수 (기본 1, 비용 절약)")
    args = parser.parse_args()

    if args.mode:
        return await run_mode(args.mode, args)

    code = 0
    while True:
        print(MENU)
        choice = ask("선택", "q").lower()
        if choice in ("q", "quit", "exit", "종료"):
            return code
        code = await run_mode(choice, args)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        raise SystemExit(130)
