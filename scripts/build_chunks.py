## *_pages.json -> 청킹 (chunk-size : 600, chunk-overlap : 100 을 default로 함)

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter


REQUIRED_METADATA = { 
    "page_id",
    "company",
    "fiscal_year",
    "statement_scope",
    "document_type",
    "source_file",
    "pdf_page",
    "section",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="페이지 JSON을 RecursiveCharacterTextSplitter로 청킹합니다."
    )
    parser.add_argument("--input-path", required=True, type=Path, help="페이지 JSON 경로")
    parser.add_argument("--output-path", required=True, type=Path, help="청크 JSON 경로")
    parser.add_argument("--chunk-size", type=int, default=600, help="청크 최대 문자 수")
    parser.add_argument("--chunk-overlap", type=int, default=100, help="인접 청크 중복 문자 수")
    return parser.parse_args()


def load_page_documents(input_path: Path) -> list[Document]: 

    documents: list[Document] = []
    with input_path.open(encoding="utf-8") as file:
        records = json.load(file)
        if not isinstance(records, list):
            raise ValueError("입력 JSON은 페이지 객체 목록이어야 합니다.")

        for record_number, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                raise ValueError(f"{record_number}번째 페이지가 JSON 객체가 아닙니다.")
            missing = REQUIRED_METADATA - record.keys() # 누락 검사 
            if missing:
                raise ValueError(f"{record_number}번째 페이지에 메타데이터가 없습니다: {sorted(missing)}")
            text = record.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{record_number}번째 페이지의 텍스트가 비어 있습니다.")

            metadata = {key: value for key, value in record.items() if key != "text"}
            documents.append(Document(page_content=text, metadata=metadata))

    if not documents:
        raise ValueError("청킹할 페이지가 없습니다.")
    return documents


def main() -> None:
    args = parse_args()
    if args.chunk_size <= 0:
        raise ValueError("chunk-size는 0보다 커야 합니다.")
    if not 0 <= args.chunk_overlap < args.chunk_size:
        raise ValueError("chunk-overlap은 0 이상 chunk-size 미만이어야 합니다.")

    page_documents = load_page_documents(args.input_path)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )
    chunks = splitter.split_documents(page_documents)

    per_page_chunk_number: defaultdict[str, int] = defaultdict(int)
    records: list[dict[str, object]] = []
    for chunk in chunks:
        page_id = chunk.metadata["page_id"]
        per_page_chunk_number[page_id] += 1
        chunk_number = per_page_chunk_number[page_id]
        chunk_id = f"{page_id}_{chunk_number:02d}"
        records.append({"chunk_id": chunk_id, "text": chunk.page_content, **chunk.metadata})

    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    with args.output_path.open("w", encoding="utf-8") as file:
        json.dump(records, file, ensure_ascii=False, indent=2)

    print(f"원본 페이지 수: {len(page_documents)}")
    print(f"만들어진 청크 수: {len(records)}")
    print(f"청크 크기 / 중복: {args.chunk_size} / {args.chunk_overlap}")
    print(f"출력 파일: {args.output_path}")


if __name__ == "__main__":
    main()
