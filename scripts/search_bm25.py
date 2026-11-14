
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.bm25_retriever import (
    load_chunk_documents,
    build_bm25_retriever,
    search_bm25,
)


DEFAULT_CHUNKS_PATH = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "dart"
    / "00126380"
    / "2025"
    / "20260310002820_chunks.json"
)


def main():
    parser = argparse.ArgumentParser(
        description="삼성전자 재무제표 BM25 검색"
    )

    parser.add_argument(
        "--chunks-path",
        type=Path,
        default=DEFAULT_CHUNKS_PATH,
        help="검색 대상 청크 JSON 파일"
    )

    parser.add_argument(
        "--question",
        required=True,
        help="검색할 질문"
    )

    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="반환할 검색 결과 개수"
    )

    args = parser.parse_args()

    documents = load_chunk_documents(args.chunks_path)
    retriever = build_bm25_retriever(documents, args.top_k)
    results = search_bm25(retriever, args.question)

    print(f"\n검색 질문: {args.question}")
    print(f"검색 대상 청크: {len(documents)}개")
    print(f"상위 검색 결과: {len(results)}개\n")

    for result in results:
        print(
            f"{result['rank']}위 | "
            f"PDF {result['pdf_page']}쪽 | "
            f"{result['chunk_id']}"
        )
        print(result["text"][:200].replace("\n", " "))
        print("-" * 60)


if __name__ == "__main__":
    main()
