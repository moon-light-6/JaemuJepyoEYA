"""KURE-v1 임베딩 기반 Chroma 벡터 DB 색인 스크립트."""

import argparse
import sys
from pathlib import Path


# 1. 프로젝트 루트 경로 설정
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.chroma_retriever import build_chroma


# 2. 기본 데이터 경로 지정
DEFAULT_CHUNKS_PATH = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "dart"
    / "00126380"
    / "2025"
    / "20260310002820_chunks.json"
)

# 3. Chroma 저장 경로 지정
DEFAULT_PERSIST_DIRECTORY = (
    PROJECT_ROOT / "data" / "chroma_samsung"
)


def main():
    parser = argparse.ArgumentParser(
        description="KURE-v1 임베딩으로 ChromaDB 색인"
    )

    parser.add_argument(
        "--chunks-path",
        type=Path,
        default=DEFAULT_CHUNKS_PATH,
        help="검색 대상 청크 JSON 파일",
    )

    parser.add_argument(
        "--persist-directory",
        type=Path,
        default=DEFAULT_PERSIST_DIRECTORY,
        help="ChromaDB 저장 폴더",
    )

    args = parser.parse_args()

    print(f"청크 파일: {args.chunks_path}")
    print(f"Chroma 저장 폴더: {args.persist_directory}")

    # 4. build_chroma 호출
    count = build_chroma(
        chunks_path=args.chunks_path,
        persist_directory=args.persist_directory,
    )

    print(f"색인 작업 종료: 총 {count}개 청크")


if __name__ == "__main__":
    main()
