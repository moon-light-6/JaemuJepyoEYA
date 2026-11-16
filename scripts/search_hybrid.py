"""BM25와 Chroma를 결합한 Hybrid-RRF 검색 실행 스크립트."""

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.hybrid_retriever import search_hybrid


DEFAULT_CHUNKS_PATH = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "dart"
    / "00126380"
    / "2025"
    / "20260310002820_chunks.json"
)

DEFAULT_PERSIST_DIRECTORY = (
    PROJECT_ROOT / "data" / "chroma_samsung"
)


def main():
    parser = argparse.ArgumentParser(
        description="BM25 + Chroma Hybrid-RRF 검색"
    )

    parser.add_argument(
        "--chunks-path",
        type=Path,
        default=DEFAULT_CHUNKS_PATH,
    )

    parser.add_argument(
        "--persist-directory",
        type=Path,
        default=DEFAULT_PERSIST_DIRECTORY,
    )

    parser.add_argument(
        "--question",
        required=True,
        help="검색할 질문",
    )

    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
    )

    parser.add_argument(
        "--candidate-k",
        type=int,
        default=10,
    )

    parser.add_argument(
        "--rank-constant",
        type=int,
        default=60,
    )

    args = parser.parse_args()

    results = search_hybrid(
        chunks_path=args.chunks_path,
        persist_directory=args.persist_directory,
        question=args.question,
        k=args.top_k,
        candidate_k=args.candidate_k,
        rank_constant=args.rank_constant,
    )

    print(f"\n검색 질문: {args.question}")
    print(
        f"검색 방식: Hybrid-RRF "
        f"(k={args.top_k}, "
        f"candidate_k={args.candidate_k}, "
        f"rank_constant={args.rank_constant})"
    )
    print(f"검색 결과: {len(results)}개\n")

    for result in results:
        print(
            f"{result['rank']}위 | "
            f"PDF {result['pdf_page']}쪽 | "
            f"{result['chunk_id']} | "
            f"RRF 점수: {result['rrf_score']:.6f}"
        )

        print(result["text"][:200].replace("\n", " "))
        print("-" * 60)


if __name__ == "__main__":
    main()
