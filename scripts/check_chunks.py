## *_pages.json 과 *_chunk.json 검증

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


REQUIRED_PAGE_FIELDS = {
    "page_id",
    "text",
    "company",
    "fiscal_year",
    "statement_scope",
    "document_type",
    "source_file",
    "pdf_page",
    "section",
}
REQUIRED_CHUNK_FIELDS = REQUIRED_PAGE_FIELDS | {"chunk_id"}
INHERITED_METADATA_FIELDS = REQUIRED_PAGE_FIELDS - {"text"}


def load_records(path: Path, label: str) -> list[dict[str, Any]]:
   
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} JSON을 읽지 못했습니다: {path} ({error})") from error

    if not isinstance(records, list):
        raise ValueError(f"{label} JSON의 최상위 값은 목록이어야 합니다: {path}")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError(f"{label} JSON의 모든 항목은 객체여야 합니다: {path}")
    return records


def record_name(record: dict[str, Any], index: int, key: str) -> str:
    """Return an identifier that remains useful even for malformed records."""
    value = record.get(key)
    return value if isinstance(value, str) and value else f"{key}-missing-at-{index}"


def validate_chunks(
    pages: list[dict[str, Any]], chunks: list[dict[str, Any]], chunk_size: int
) -> dict[str, Any]:
    """Return a reproducible chunk-quality report without changing input data."""
    if chunk_size <= 0:
        raise ValueError("chunk-size는 0보다 커야 합니다.")

    page_missing_fields: list[dict[str, Any]] = []
    page_ids: list[str] = []
    for index, page in enumerate(pages, start=1):
        missing = sorted(REQUIRED_PAGE_FIELDS - page.keys())
        if missing:
            page_missing_fields.append({"index": index, "missing_fields": missing})
        page_id = page.get("page_id")
        if isinstance(page_id, str) and page_id:
            page_ids.append(page_id)

    duplicate_page_ids = sorted(
        page_id for page_id, count in Counter(page_ids).items() if count > 1
    )
    pages_by_id = {
        page["page_id"]
        for page in pages
        if isinstance(page.get("page_id"), str) and page["page_id"] not in duplicate_page_ids
    }
    page_lookup = {
        page["page_id"]: page
        for page in pages
        if isinstance(page.get("page_id"), str) and page["page_id"] in pages_by_id
    }

    chunk_ids: list[str] = []
    for chunk in chunks:
        chunk_id = chunk.get("chunk_id")
        if isinstance(chunk_id, str) and chunk_id:
            chunk_ids.append(chunk_id)
    duplicate_chunk_ids = sorted(
        chunk_id for chunk_id, count in Counter(chunk_ids).items() if count > 1
    )

    blank_chunk_ids: list[str] = []
    missing_chunk_metadata: list[dict[str, Any]] = []
    oversized_chunk_ids: list[dict[str, Any]] = []
    unknown_page_chunk_ids: list[dict[str, Any]] = []
    metadata_mismatches: list[dict[str, Any]] = []
    text_not_in_source_chunk_ids: list[str] = []
    page_ids_with_chunks: set[str] = set()

    for index, chunk in enumerate(chunks, start=1):
        chunk_id = record_name(chunk, index, "chunk_id")
        missing = sorted(REQUIRED_CHUNK_FIELDS - chunk.keys())
        if missing:
            missing_chunk_metadata.append({"chunk_id": chunk_id, "missing_fields": missing})
            continue

        text = chunk["text"]
        if not isinstance(text, str) or not text.strip():
            blank_chunk_ids.append(chunk_id)
            continue
        if len(text) > chunk_size:
            oversized_chunk_ids.append({"chunk_id": chunk_id, "character_count": len(text)})

        page_id = chunk["page_id"]
        source_page = page_lookup.get(page_id) if isinstance(page_id, str) else None
        if source_page is None:
            unknown_page_chunk_ids.append({"chunk_id": chunk_id, "page_id": page_id})
            continue

        page_ids_with_chunks.add(page_id)
        different_fields = [
            field
            for field in INHERITED_METADATA_FIELDS
            if chunk.get(field) != source_page.get(field)
        ]
        if different_fields:
            metadata_mismatches.append(
                {"chunk_id": chunk_id, "page_id": page_id, "fields": different_fields}
            )
        if text not in source_page["text"]:
            text_not_in_source_chunk_ids.append(chunk_id)

    pages_without_chunks = sorted(set(page_lookup) - page_ids_with_chunks)
    violations = {
        "page_missing_fields": page_missing_fields,
        "duplicate_page_ids": duplicate_page_ids,
        "blank_chunk_ids": blank_chunk_ids,
        "duplicate_chunk_ids": duplicate_chunk_ids,
        "missing_chunk_metadata": missing_chunk_metadata,
        "oversized_chunk_ids": oversized_chunk_ids,
        "unknown_page_chunk_ids": unknown_page_chunk_ids,
        "metadata_mismatches": metadata_mismatches,
        "text_not_in_source_chunk_ids": text_not_in_source_chunk_ids,
        "pages_without_chunks": pages_without_chunks,
    }
    passed = not any(violations.values())
    return {
        "status": "passed" if passed else "failed",
        "settings": {"chunk_size": chunk_size},
        "summary": {
            "page_count": len(pages),
            "chunk_count": len(chunks),
            "pages_represented_by_chunks": len(page_ids_with_chunks),
        },
        "violations": violations,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="페이지와 청크 JSON의 품질을 검증합니다.")
    parser.add_argument("--pages-path", required=True, type=Path, help="페이지 JSON 경로")
    parser.add_argument("--chunks-path", required=True, type=Path, help="청크 JSON 경로")
    parser.add_argument("--output-path", required=True, type=Path, help="검증 결과 JSON 경로")
    parser.add_argument("--chunk-size", type=int, default=600, help="허용할 최대 청크 문자 수")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pages = load_records(args.pages_path, "페이지")
    chunks = load_records(args.chunks_path, "청크")
    result = validate_chunks(pages, chunks, args.chunk_size)

    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"페이지 수: {result['summary']['page_count']}")
    print(f"청크 수: {result['summary']['chunk_count']}")
    print(f"청크가 포함된 페이지 수: {result['summary']['pages_represented_by_chunks']}")
    print(f"검증 상태: {result['status']}")
    print(f"검증 결과: {args.output_path}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
