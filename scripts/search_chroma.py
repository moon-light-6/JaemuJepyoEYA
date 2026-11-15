"""Chroma 벡터 유사도 검색 실행 스크립트."""

import argparse
import sys
from pathlib import Path


# 1. 프로젝트 루트 경로 설정
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.chroma_retriever import search_chroma


# 2. Chroma 저장 경로
DEFAULT_PERSIST_DIRECTORY = (
    PROJECT_ROOT / "data" / "chroma_samsung"
)


def main():
    parser = argparse.ArgumentParser(
        description="KURE-v1 기반 Chroma 벡터 검색"
    )

    parser.add_argument(
        "--persist-directory",
        type=Path,
        default=DEFAULT_PERSIST_DIRECTORY,
        help="ChromaDB 저장 폴더",
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
        help="반환할 검색 결과 개수",
    )

    args = parser.parse_args()

    # 3. Chroma 유사도 검색
    results = search_chroma(
        persist_directory=args.persist_directory,
        question=args.question,
        k=args.top_k,
    )

    # 4. 검색 결과 출력
    print(f"\n검색 질문: {args.question}")
    print(f"검색 결과: {len(results)}개\n")

    for result in results:
        print(
            f"{result['rank']}위 | "
            f"PDF {result['pdf_page']}쪽 | "
            f"{result['chunk_id']} | "
            f"거리: {result['distance']:.4f}"
        )

        print(
            result["text"][:200].replace("\n", " ")
        )

        print("-" * 60)


if __name__ == "__main__":
    main()
