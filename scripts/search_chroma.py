"""기업별 Chroma 벡터 유사도 검색 실행 스크립트."""

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.chroma_retriever import search_chroma


DEFAULT_PERSIST_DIRECTORY = (
    PROJECT_ROOT / "data" / "chroma_multi"
)


def main():
    parser = argparse.ArgumentParser(
        description="기업별 KURE-v1 Chroma 검색"
    )

    parser.add_argument("--corp-code", required=True)
    parser.add_argument("--fiscal-year", required=True, type=int)
    parser.add_argument(
        "--statement-scope",
        required=True,
        choices=["consolidated", "separate"],
    )
    parser.add_argument("--rcept-no", required=True)

    parser.add_argument(
        "--persist-directory",
        type=Path,
        default=DEFAULT_PERSIST_DIRECTORY,
    )
    parser.add_argument("--question", required=True)
    parser.add_argument("--top-k", type=int, default=5)

    args = parser.parse_args()

    target = SearchTarget(
        corp_code=args.corp_code,
        fiscal_year=args.fiscal_year,
        statement_scope=args.statement_scope,
        rcept_no=args.rcept_no,
    )

    results = search_chroma(
        persist_directory=args.persist_directory,
        question=args.question,
        collection_name=target.collection_name,
        k=args.top_k,
    )

    print(f"\n컬렉션: {target.collection_name}")
    print(f"검색 질문: {args.question}")
    print(f"검색 결과: {len(results)}개\n")

    for result in results:
        print(
            f"{result['rank']}위 | "
            f"PDF {result['pdf_page']}쪽 | "
            f"{result['chunk_id']} | "
            f"거리: {result['distance']:.4f}"
        )
        print(result["text"][:200].replace("\n", " "))
        print("-" * 60)


if __name__ == "__main__":
    main()
