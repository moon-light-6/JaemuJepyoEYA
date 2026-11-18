"""1회 검색 또는 근거 평가와 재검색을 포함한 조사 루프를 실행합니다."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.research.loop.graph import run_once, run_research_loop
from fin_research.progress import TerminalProgress, report_progress


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True)
    parser.add_argument("--top-k", type=int, choices=range(1, 11), default=5)
    parser.add_argument("--model", choices=["small", "large"], default="small")
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument(
        "--max-rounds", type=int, choices=range(1, 6),
        help="지정하면 근거 평가·재검색 실행. 최대 검색 횟수 1~5회",
    )
    parser.add_argument(
        "--persist-directory", type=Path,
        default=PROJECT_ROOT / "data/chroma_samsung",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--quiet", action="store_true", help="조사 단계 진행 로그 숨기기")
    args = parser.parse_args()
    if args.search_only and args.max_rounds is not None:
        parser.error("--search-only와 --max-rounds는 함께 사용할 수 없습니다.")
    progress = None if args.quiet else TerminalProgress()
    try:
        if args.max_rounds is not None:
            result = run_research_loop(
                args.question, args.persist_directory, args.top_k,
                args.model, args.max_rounds,
                progress=progress,
            )
        else:
            result = run_once(
                args.question, args.persist_directory, args.top_k,
                args.model, args.search_only,
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
