"""PDF 검증 데모: AI가 쓴 재무 보고서 PDF의 숫자 주장을 DART DB(Supabase)와 대조한다.

실행 (프로젝트 루트, .env에 OPENAI_API_KEY·DATABASE_URL 필요):
    python scripts/demo/demo_verify.py --pdf 보고서.pdf --company NAVER
    python scripts/demo/demo_verify.py --pdf data/processed/uploads/{job_id}_pages.json --company NAVER   # API 추출 결과
    python scripts/demo/demo_verify.py --pdf 보고서.pdf --company NAVER --json 결과.json                  # React용 JSON 저장
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# 패키지를 별도 설치하지 않아도 프로젝트 루트에서 실행할 수 있도록 한다.
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # scripts/demo/ → 프로젝트 루트
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.llm.budget import Budget
from fin_research.verify.graph import build_graph, run_config

MARK = {"match": "✅", "mismatch": "❌", "not_found": "❓", "unverifiable": "➖"}
LABEL = {"match": "일치", "mismatch": "불일치", "not_found": "근거 없음", "unverifiable": "검증 불가"}


def claim_value(claim) -> str:
    if claim.value is None:
        return "-"
    value = f"{claim.value:,.0f}" if float(claim.value).is_integer() else f"{claim.value:,}"
    return f"{value}{claim.unit or ''}"


def print_result(result: dict) -> None:
    print(f"기업: {result.get('company')} ({result.get('corp_code')})  |  "
          f"사업연도 {result.get('fiscal_year')}  |  공시 {result.get('rcept_no')}")
    claims = {c.id: c for c in result.get("claims", [])}
    if claims:
        print()
        print(f"{'':2} {'ID':4} {'쪽':>3}  {'항목':10} {'주장':>14}  {'판정':6}  설명")
    for verdict in result.get("verdicts", []):
        claim = claims[verdict.claim_id]
        page = str(claim.page) if claim.page else "-"
        print(f"{MARK[verdict.label]} {verdict.claim_id:4} {page:>3}  {claim.item:10} {claim_value(claim):>14}  "
              f"{LABEL[verdict.label]:6}  {verdict.explanation}")
    summary = result.get("summary") or {}
    print("\n요약: " + ", ".join(f"{LABEL[k]} {summary.get(k, 0)}" for k in LABEL) + f"  (총 {summary.get('total', 0)}개)")
    for error in result.get("errors", []):
        print(f"⚠ {error}")


def to_json(result: dict) -> dict:
    """React 팀에 넘길 형태: Pydantic 모델을 dict로."""
    return {
        **{k: result.get(k) for k in ("company", "corp_code", "fiscal_year", "rcept_no", "summary", "errors")},
        "claims": [c.model_dump() for c in result.get("claims", [])],
        "verdicts": [v.model_dump() for v in result.get("verdicts", [])],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="PDF 숫자 주장 검증 데모")
    parser.add_argument("--pdf", required=True, help="보고서 PDF 또는 API 추출 JSON({job_id}_pages.json)")
    parser.add_argument("--company", required=True, help="기업명 (예: NAVER, KB금융)")
    parser.add_argument("--json", help="결과를 JSON 파일로 저장할 경로")
    args = parser.parse_args()

    if not Path(args.pdf).is_file():
        parser.error(f"파일이 없습니다: {args.pdf}")
    budget = Budget.from_config()
    started = time.perf_counter()
    result = await build_graph().ainvoke({"pdf_path": args.pdf, "company": args.company}, run_config(budget))
    print_result(result)

    usage = budget.summary()
    print(f"\nLLM {usage['calls']}회 · {usage['total_tokens']:,}토큰 · {time.perf_counter() - started:.1f}초")
    if args.json:
        Path(args.json).write_text(json.dumps(to_json(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"JSON 저장: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
