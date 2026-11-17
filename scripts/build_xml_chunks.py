"""검색용 XML 문서 JSON을 RecursiveCharacterTextSplitter로 청킹한다."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter


REQUIRED_METADATA = {
    "page_id", "company", "fiscal_year", "statement_scope", "document_type",
    "source_format", "source_file", "source_xpath", "source_element_index", "section", "title",
}


def build_xml_chunks(records: list[dict[str, Any]], chunk_size: int, chunk_overlap: int) -> list[dict[str, Any]]:
    """XML 위치 메타데이터를 보존한 채 검색용 청크를 만든다."""

    documents: list[Document] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"{index}번째 XML 문서가 JSON 객체가 아닙니다.")
        missing = REQUIRED_METADATA - record.keys()
        if missing:
            raise ValueError(f"{index}번째 XML 문서에 메타데이터가 없습니다: {sorted(missing)}")
        if record.get("source_format") != "xml":
            raise ValueError(f"{index}번째 문서의 source_format은 xml이어야 합니다.")
        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{index}번째 XML 문서의 텍스트가 비어 있습니다.")
        documents.append(Document(page_content=text, metadata={key: value for key, value in record.items() if key != "text"}))

    if not documents:
        raise ValueError("청킹할 XML 문서가 없습니다.")
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = splitter.split_documents(documents)
    per_document: defaultdict[str, int] = defaultdict(int)
    result: list[dict[str, Any]] = []
    for chunk in chunks:
        page_id = str(chunk.metadata["page_id"])
        per_document[page_id] += 1
        result.append({
            "chunk_id": f"{page_id}_{per_document[page_id]:02d}",
            "text": chunk.page_content,
            **chunk.metadata,
        })
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="XML 문서 JSON을 검색용 청크 JSON으로 만듭니다.")
    parser.add_argument("--input-path", required=True, type=Path)
    parser.add_argument("--output-path", required=True, type=Path)
    parser.add_argument("--chunk-size", type=int, default=600)
    parser.add_argument("--chunk-overlap", type=int, default=100)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.chunk_size <= 0:
        raise ValueError("chunk-size는 0보다 커야 합니다.")
    if not 0 <= args.chunk_overlap < args.chunk_size:
        raise ValueError("chunk-overlap은 0 이상 chunk-size 미만이어야 합니다.")
    records = json.loads(args.input_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("입력 JSON은 XML 문서 객체 목록이어야 합니다.")
    chunks = build_xml_chunks(records, args.chunk_size, args.chunk_overlap)
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(chunks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"원본 XML 문서 수: {len(records)}")
    print(f"만들어진 XML 청크 수: {len(chunks)}")
    print(f"청크 크기 / 중복: {args.chunk_size} / {args.chunk_overlap}")
    print(f"출력 파일: {args.output_path}")


if __name__ == "__main__":
    main()
