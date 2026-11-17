"""1회 검색 또는 근거 평가와 재검색을 포함한 조사 루프를 실행합니다."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.research.loop.graph import run_once, run_research_loop

LEGACY_PERSIST_DIRECTORY = PROJECT_ROOT / "data/chroma_samsung"  # 기존 단일 컬렉션
MULTI_PERSIST_DIRECTORY = PROJECT_ROOT / "data/chroma_multi"     # build_chroma.py 기업별 컬렉션


def resolve_target(args, parser):
    """--company/--corp-code(+연도·접수번호)로 SearchTarget을 만든다. 둘 다 없으면 None(기존 방식)."""
    if not (args.company or args.corp_code):
        return None

    from fin_research.companies import available_years, find_companies, load_companies
    from fin_research.retrieval.search_target import SearchTarget

    if args.corp_code:
        matches = [c for c in load_companies() if c.get("corp_code") == args.corp_code]
    else:
        matches = find_companies(args.company)
    if len(matches) != 1:
        names = ", ".join(c["name"] for c in matches) or "없음"
        parser.error(f"기업을 하나로 정할 수 없습니다: {args.company or args.corp_code} → {names}")
    company = matches[0]

    years = available_years(company)
    fiscal_year = args.fiscal_year or (max(years) if years else None)
    report = (company.get("reports") or {}).get(str(fiscal_year)) or {}
    rcept_no = args.rcept_no or report.get("rcept_no")
    if not fiscal_year or not rcept_no:
        parser.error(
            f"{company['name']} {fiscal_year}년 접수번호(rcept_no)를 companies.yaml에서 찾지 못했습니다. "
            "--rcept-no로 지정하세요."
        )
    return SearchTarget(
        corp_code=company["corp_code"], fiscal_year=int(fiscal_year),
        statement_scope=args.statement_scope, rcept_no=rcept_no,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True)
    parser.add_argument("--top-k", type=int, choices=range(1, 11), default=5)
    parser.add_argument(
        "--model", choices=["small", "large"], default=None,
        help="생략하면 models.yaml roles(loop_evaluator·loop_findings) 설정을 따름",
    )
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument(
        "--max-rounds", type=int, choices=range(1, 6),
        help="지정하면 근거 평가·재검색 실행. 최대 검색 횟수 1~5회",
    )
    target_group = parser.add_argument_group(
        "검색 대상 (지정하면 build_chroma.py가 만든 기업별 컬렉션을 검색)"
    )
    target_group.add_argument("--company", help="기업명 (companies.yaml 기준). 예: 삼성전자")
    target_group.add_argument("--corp-code", help="DART 기업 고유번호 8자리")
    target_group.add_argument("--fiscal-year", type=int, help="생략하면 수집된 최신 사업연도")
    target_group.add_argument(
        "--statement-scope", choices=["consolidated", "separate"], default="consolidated",
    )
    target_group.add_argument("--rcept-no", help="생략하면 companies.yaml의 접수번호")
    parser.add_argument(
        "--persist-directory", type=Path, default=None,
        help=f"생략하면 검색 대상 지정 시 {MULTI_PERSIST_DIRECTORY.name}, 아니면 {LEGACY_PERSIST_DIRECTORY.name}",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.search_only and args.max_rounds is not None:
        parser.error("--search-only와 --max-rounds는 함께 사용할 수 없습니다.")

    target = resolve_target(args, parser)
    persist_directory = args.persist_directory or (
        MULTI_PERSIST_DIRECTORY if target else LEGACY_PERSIST_DIRECTORY
    )
    try:
        if args.max_rounds is not None:
            result = run_research_loop(
                args.question, persist_directory, args.top_k,
                args.model, args.max_rounds, target=target,
            )
        else:
            result = run_once(
                args.question, persist_directory, args.top_k,
                args.model, args.search_only, target=target,
            )
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"실행 실패: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
