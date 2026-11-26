"""PDF 검증 데모: AI가 쓴 재무 보고서 PDF의 숫자 주장을 DART DB(Supabase)와 대조한다.

실행 (프로젝트 루트, DB 설정과 선택한 모델 서버 또는 OpenAI 키 필요):
    # Qwen 공개 모델: GPU PC의 모델 서버를 먼저 실행한다.
    python scripts/demo/demo_verify.py --pdf 보고서.pdf --company NAVER --claim-model qwen --json Qwen_결과.json
    # 기존 GPT 모델: OpenAI API 키를 사용하며 기존 코드를 비교 실행할 수 있다.
    python scripts/demo/demo_verify.py --pdf 보고서.pdf --company NAVER --claim-model small --json GPT_결과.json
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
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.llm.budget import Budget
from fin_research.llm.client import load_model_config
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
        # Qwen과 GPT의 결과 파일을 구분할 수 있도록 실제 선택한 모델을 남긴다.
        "model_info": result.get("model_info"),
    }


async def main() -> int:
    """선택한 주장 추출 모델로 검증 그래프를 실행하고 결과와 사용량을 저장한다."""
    parser = argparse.ArgumentParser(description="PDF 숫자 주장 검증 데모")
    parser.add_argument("--pdf", required=True, help="보고서 PDF 또는 API 추출 JSON({job_id}_pages.json)")
    parser.add_argument("--company", required=True, help="기업명 (예: NAVER, KB금융)")
    parser.add_argument("--json", help="결과를 JSON 파일로 저장할 경로")
    # 기존 GPT 모델은 small로 명시해서 다시 실행할 수 있다. 기본은 역할 설정의 Qwen이다.
    parser.add_argument("--claim-model", choices=["qwen", "small"], default=None,
                        help="주장 추출 모델: qwen=공개 모델, small=기존 GPT 모델. 생략하면 models.yaml 설정")
    args = parser.parse_args()

    if not Path(args.pdf).is_file():
        parser.error(f"파일이 없습니다: {args.pdf}")
    budget = Budget.from_config()
    started = time.perf_counter()
    selected = args.claim_model or load_model_config()["roles"]["claim_extractor"]["model"]
    model_name = load_model_config()["models"][selected]["model"]
    print(f"주장 추출 모델: {model_name} ({'Qwen 공개 모델' if selected == 'qwen' else '기존 GPT 모델'})")
    # 기존 GPT 실행 보존: 역할 기본값이 small이던 시기의 호출 방식이다.
    # result = await build_graph().ainvoke({"pdf_path": args.pdf, "company": args.company}, run_config(budget))
    # Qwen 전환: 선택한 모델 키만 전달하고 DB 조회·수치 검증 그래프는 그대로 재사용한다.
    from openai import APIConnectionError, APIStatusError
    try:
        result = await build_graph().ainvoke(
            {"pdf_path": args.pdf, "company": args.company},
            run_config(budget, claim_model_key=selected),
        )
    except (APIConnectionError, APIStatusError) as exc:
        # 연결 실패를 숨기거나 유료 GPT로 자동 전환하지 않고 필요한 조치를 안내한다.
        if selected == "qwen":
            print("Qwen 호출 실패: 모델 서버 실행 상태, QWEN_BASE_URL, QWEN_API_KEY 및 서버 로그를 확인하세요. "
                  "먼저 python -X utf8 scripts/check_qwen.py --smoke를 실행하세요.", file=sys.stderr)
        else:
            print("GPT 호출 실패: OpenAI API 키·연결·사용 한도를 확인하세요.", file=sys.stderr)
        print(f"오류 유형: {type(exc).__name__}", file=sys.stderr)
        return 2
    result["model_info"] = {"key": selected, "model": model_name}
    print_result(result)

    usage = budget.summary()
    print(f"\nLLM {usage['calls']}회 · {usage['total_tokens']:,}토큰 · {time.perf_counter() - started:.1f}초")
    if args.json:
        # Qwen과 GPT 비교 결과를 별도 폴더에 저장할 때 상위 폴더도 준비한다.
        output = Path(args.json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(to_json(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"JSON 저장: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
