"""1회 검색 또는 근거 평가와 재검색을 포함한 조사 루프를 실행합니다."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.research.loop.graph import run_once, run_research_loop
from fin_research.progress import TerminalProgress, report_progress

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
    parser.add_argument("--multi-tool", action="store_true", help="SQL·계산·문서 검색을 선택하는 다중 도구 조사")
    parser.add_argument("--xml-version", type=int, choices=[1, 2], default=1, help="다중 도구 XML 검색의 청크 버전")
    parser.add_argument("--source-format", choices=["xml", "pdf"], default="xml",
                        help="다중 도구 설명 검색에서 사용할 색인 형식 (기본 xml)")
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
    parser.add_argument("--quiet", action="store_true", help="조사 단계 진행 로그 숨기기")
    args = parser.parse_args()
    if args.search_only and args.max_rounds is not None:
        parser.error("--search-only와 --max-rounds는 함께 사용할 수 없습니다.")
    if args.multi_tool and args.search_only:
        parser.error("--multi-tool과 --search-only는 함께 사용할 수 없습니다.")
    if args.xml_version == 2 and (not args.multi_tool or args.source_format != "xml"):
        parser.error("--xml-version 2는 --multi-tool --source-format xml에서만 사용할 수 있습니다.")
    target = resolve_target(args, parser)
    if args.multi_tool and target is None:
        parser.error("다중 도구 조사에는 --company 또는 --corp-code와 공시 기준이 필요합니다.")
    persist_directory = args.persist_directory or (
        PROJECT_ROOT / "data/chroma_xml_v2" if args.xml_version == 2 else
        MULTI_PERSIST_DIRECTORY if target else LEGACY_PERSIST_DIRECTORY
    )
    progress = None if args.quiet else TerminalProgress()
    try:
        if args.multi_tool:
            from dotenv import load_dotenv
            from fin_research.companies import load_companies
            from fin_research.research.loop.tool_graph import run_tool_research
            from fin_research.tools.financial_models import FinancialContext

            load_dotenv(PROJECT_ROOT / ".env")
            company = next(c["name"] for c in load_companies() if c["corp_code"] == target.corp_code)
            context = FinancialContext(company=company, corp_code=target.corp_code, year=target.fiscal_year,
                                       scope=target.statement_scope, rcept_no=target.rcept_no)
            result = run_tool_research(
                args.question, context, persist_directory, model_name=args.model,
                max_rounds=args.max_rounds or 3, top_k=args.top_k, progress=progress, source_format=args.source_format, xml_version=args.xml_version,
            )
        elif args.max_rounds is not None:
            result = run_research_loop(
                args.question, persist_directory, args.top_k,
                args.model, args.max_rounds, target=target,
                progress=progress,
            )
        else:
            result = run_once(
                args.question, persist_directory, args.top_k,
                args.model, args.search_only, target=target,
                progress=progress,
            )
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"실행 실패: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
        report_progress(progress, f"결과 JSON 저장: {args.out.resolve()}")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
