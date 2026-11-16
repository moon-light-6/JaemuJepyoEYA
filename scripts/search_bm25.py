"""BM25 키워드 검색 실행 스크립트."""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.bm25_retriever import (
    load_chunk_documents,
    build_bm25_retriever,
    search_bm25,
    search_bm25_for_target
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
        description="기업별 재무제표 BM25 검색"
    )

    # 기존 검색 방식 유지
    parser.add_argument(
        "--chunks-path",
        type=Path,
        default=None,
        help="검색할 청크 JSON 경로",
    )

    # 기업별 검색 대상 지정
    parser.add_argument("--corp-code", help="DART 기업고유번호")
    parser.add_argument("--fiscal-year", type=int, help="사업연도")
    parser.add_argument(
        "--statement-scope",
        choices=["consolidated", "separate"],
        help="연결/별도 기준",
    )
    parser.add_argument("--rcept-no", help="DART 접수번호")

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

    target_values = [
        args.corp_code,
        args.fiscal_year,
        args.statement_scope,
        args.rcept_no,
    ]

    has_target = any(value is not None for value in target_values)
    complete_target = all(value is not None for value in target_values)

    # 일부 필드만 입력한 경우 방지
    if has_target and not complete_target:
        parser.error(
            "기업별 검색에는 --corp-code, --fiscal-year, "
            "--statement-scope, --rcept-no가 모두 필요합니다."
        )

    # 기업 선택과 수동 경로 지정의 충돌 방지
    if has_target and args.chunks_path is not None:
        parser.error(
            "기업별 검색 옵션과 --chunks-path를 함께 지정할 수 없습니다."
        )

    if args.top_k <= 0:
        parser.error("--top-k는 1 이상이어야 합니다.")

    if complete_target:
        # 13-3: 새로운 기업별 검색 방식
        target = SearchTarget(
            corp_code=args.corp_code,
            fiscal_year=args.fiscal_year,
            statement_scope=args.statement_scope,
            rcept_no=args.rcept_no,
        )

        results = search_bm25_for_target(
            target=target,
            project_root=PROJECT_ROOT,
            question=args.question,
            k=args.top_k,
        )

        print(f"기업고유번호: {target.corp_code}")
        print(f"사업연도: {target.fiscal_year}")
        print(f"연결/별도: {target.statement_scope}")
        print(f"접수번호: {target.rcept_no}")
        print(f"청크 파일: {target.chunks_path(PROJECT_ROOT)}")

    else:
        # 기존 방식: 지정된 파일 또는 삼성전자 기본 파일 사용
        chunks_path = args.chunks_path or DEFAULT_CHUNKS_PATH

        documents = load_chunk_documents(chunks_path)
        retriever = build_bm25_retriever(
            documents,
            k=args.top_k,
        )
        results = search_bm25(
            retriever,
            args.question,
        )

        print(f"청크 파일: {chunks_path}")

    print(f"\n검색 질문: {args.question}")
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
