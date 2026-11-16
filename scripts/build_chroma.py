"""SearchTarget 기반 Chroma 벡터 DB 색인 스크립트."""

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.bm25_retriever import load_chunk_documents
from fin_research.retrieval.chroma_retriever import build_chroma


DEFAULT_PERSIST_DIRECTORY = (
    PROJECT_ROOT / "data" / "chroma_multi"
)


def main():
    parser = argparse.ArgumentParser(
        description="기업별 KURE-v1 ChromaDB 색인"
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

    args = parser.parse_args()

    # 1. 검색 대상 생성
    target = SearchTarget(
        corp_code=args.corp_code,
        fiscal_year=args.fiscal_year,
        statement_scope=args.statement_scope,
        rcept_no=args.rcept_no,
    )

    # 2. 청크 파일 및 컬렉션 이름 결정
    chunks_path = target.chunks_path(PROJECT_ROOT)
    collection_name = target.collection_name

    # 3. 청크 파일 확인
    if not chunks_path.is_file():
        raise FileNotFoundError(
            f"청크 파일이 없습니다: {chunks_path}"
        )

    # 4. 색인 전 검색 대상 검증
    documents = load_chunk_documents(chunks_path)

    for document in documents:
        metadata = document.metadata

        if metadata["fiscal_year"] != target.fiscal_year:
            raise ValueError(
                f"사업연도 불일치: {metadata['chunk_id']}"
            )

        if metadata["statement_scope"] != target.statement_scope:
            raise ValueError(
                f"연결/별도 불일치: {metadata['chunk_id']}"
            )

        if metadata["source_file"] != f"{target.rcept_no}.pdf":
            raise ValueError(
                f"공시 파일 불일치: {metadata['chunk_id']}"
            )

    # 5. 색인 실행
    print(f"검색 대상: {target}")
    print(f"청크 파일: {chunks_path}")
    print(f"컬렉션 이름: {collection_name}")
    print(f"저장 폴더: {args.persist_directory}")

    count = build_chroma(
        chunks_path=chunks_path,
        persist_directory=args.persist_directory,
        collection_name=collection_name,
    )

    print(f"색인 작업 종료: 총 {count}개 청크")


if __name__ == "__main__":
    main()
