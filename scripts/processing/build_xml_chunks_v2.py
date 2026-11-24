"""검색용 XML 문서 JSON을 RecursiveCharacterTextSplitter로 청킹한다."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

# from langchain_core.documents import Document # Document 사용하지 않음 
from langchain_text_splitters import RecursiveCharacterTextSplitter


REQUIRED_METADATA = {
    "page_id", "company", "fiscal_year", "statement_scope", "document_type",
    "source_format", "source_file", "source_xpath", "source_element_index", "section", "title",
}


def normalize_search_text(text: str) -> str:
    """DART 표의 글자 단위 공백을 검색에 덜 방해되게 정리한다.

    원문은 바꾸지 않고, 계정명처럼 한글 글자 사이에만 공백이 반복된 경우에만
    붙인다. 예: ``자 산 총 계`` → ``자산총계``.
    """

    return re.sub(r"(?<!\S)(?:[가-힣]\s+){2,}[가-힣](?!\S)",
                  lambda match: re.sub(r"\s+", "", match.group(0)), text)


def build_xml_chunks(
    records: list[dict[str, Any]],
    chunk_size: int,
    chunk_overlap: int,
) -> list[dict[str, Any]]:
    """재무제표는 행 단위로, 주석은 기존 방식으로 청킹한다."""
    if not records:
        raise ValueError("청킹할 XML 문서가 없습니다.")

    if chunk_size <= 0:
        raise ValueError("chunk_size는 0보다 커야 합니다.")

    if not 0 <= chunk_overlap < chunk_size:
        raise ValueError(
            "chunk_overlap은 0 이상 chunk_size 미만이어야 합니다."
        )

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )

    per_document: defaultdict[str, int] = defaultdict(int)
    result: list[dict[str, Any]] = []

    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(
                f"{index}번째 XML 문서가 JSON 객체가 아닙니다."
            )

        missing = REQUIRED_METADATA - record.keys()
        if missing:
            raise ValueError(
                f"{index}번째 XML 문서에 메타데이터가 없습니다: "
                f"{sorted(missing)}"
            )

        if record.get("source_format") != "xml":
            raise ValueError(
                f"{index}번째 문서의 source_format은 xml이어야 합니다."
            )

        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                f"{index}번째 XML 문서의 텍스트가 비어 있습니다."
            )

        if record["document_type"] == "xml_financial_statement":
            # 재무제표는 머리말을 반복하고 행 단위로 분할한다.
            header = build_statement_header(record)
            pieces = split_statement_rows(
                text,
                header,
                chunk_size,
                chunk_overlap,
            )
        else:
            # 주석은 기존 RecursiveCharacterTextSplitter를 사용한다.
            context = (
                f"[검색 문맥 | {record['section']} | {record['title']}]\n"
            )
            pieces = splitter.split_text(
                context + normalize_search_text(text)
            )

        metadata = {
            key: value
            for key, value in record.items()
            if key != "text"
        }
        page_id = str(record["page_id"])

        for piece in pieces:
            per_document[page_id] += 1

            result.append({
                **metadata,
                "chunk_id": f"{page_id}_{per_document[page_id]:02d}",
                "parent_id": page_id,
                "text": piece,
            })

    return result


def build_statement_header(record: dict[str, Any]) -> str:
    """재무제표의 회사, 기준, 단위와 열 정보를 머리말로 만든다."""
    lines = [
        line.strip()
        for line in record["text"].splitlines()
        if line.strip()
    ]

    scope_labels = {
        "consolidated": "연결",
        "separate": "별도",
    }
    scope = scope_labels.get(
        record["statement_scope"],
        record["statement_scope"],
    )

    header_parts = [ 
        record["company"],
        f"{record['fiscal_year']}년 {scope}",
        record["title"],
    ]

    period_years = {}
    unit = None
    column_headers = []

    for line in lines:
        # 예: 제 57 기 : 2025년 → 57기와 2025년을 연결한다.
        period_match = re.search(
            r"제\s*(\d+)\s*(?:\([^)]*\)\s*)?기(?:말)?"
            r"\s*[:：]?\s*((?:19|20)\d{2})",
            line,
        )
        if period_match:
            period = period_match.group(1)
            year = period_match.group(2)
            period_years[period] = year

        # 예: (단위 : 백만원)에서 백만원을 읽는다.
        if unit is None:
            unit_match = re.search(
                r"단위\s*[:：]\s*(백만원|천원|억원|원)",
                line,
            )
            if unit_match:
                unit = unit_match.group(1)

        # 표 열 제목은 보통 '과목 | 주석 | 제57기 | 제56기' 형태다.
        if "|" in line and not column_headers:
            cells = [cell.strip() for cell in line.split("|")]
            first_cell = re.sub(r"\s+", "", cells[0])

            if first_cell in {"과목", "구분", "항목", "계정과목"}:
                column_headers = cells

        # 표의 본문이 시작되면 뒤쪽 주석의 단위를 읽지 않는다.
        if "|" in line:
            cells = [cell.strip() for cell in line.split("|")]
            has_number = any(
                re.fullmatch(r"\(?-?[\d,]+\)?", cell)
                for cell in cells[1:]
            )
            if has_number:
                break

    if unit:
        header_parts.append(f"단위: {unit}")

    if column_headers:
        labels = []

        for cell in column_headers:
            period_match = re.search(
                r"제\s*(\d+)\s*(?:\([^)]*\)\s*)?기(?:말)?",
                cell,
            )

            if period_match and period_match.group(1) in period_years:
                period = period_match.group(1)
                year = period_years[period]
                labels.append(f"제{period}기({year}년)")
            else:
                labels.append(normalize_search_text(cell))

        header_parts.append("열: " + ", ".join(labels))

    return "[" + " | ".join(header_parts) + "]"


def split_statement_rows(
    text: str,
    header: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """재무제표의 행을 자르지 않고 청크를 만든다."""
    chunks = []
    current_rows = []

    # 청크 목표 길이에서 머리말과 줄바꿈이 차지하는 공간을 뺀다.
    body_budget = chunk_size - len(header) - 1

    for original_line in text.splitlines():
        line = normalize_search_text(original_line.strip())

        if not line:
            continue

        candidate_rows = current_rows + [line]
        candidate_length = len("\n".join(candidate_rows))

        # 다음 행을 넣으면 목표 크기를 넘는 경우 현재 청크를 저장한다.
        if current_rows and candidate_length > body_budget:
            chunk_text = header + "\n" + "\n".join(current_rows)
            chunks.append(chunk_text)

            # 이전 청크의 끝에서 완전한 행만 골라 다음 청크에 겹친다.
            overlap_rows = []
            overlap_length = 0

            for previous_row in reversed(current_rows):
                separator_length = 1 if overlap_rows else 0
                next_overlap_length = (
                    overlap_length
                    + separator_length
                    + len(previous_row)
                )

                if next_overlap_length > chunk_overlap:
                    break

                # 겹친 행과 새 행이 목표 크기에 들어가는지 확인한다.
                if next_overlap_length + 1 + len(line) > body_budget:
                    break

                overlap_rows.append(previous_row)
                overlap_length = next_overlap_length

            current_rows = list(reversed(overlap_rows))

        # 행 하나가 목표 크기를 넘어도 중간에서 자르지 않는다.
        current_rows.append(line)

    # 마지막으로 쌓아 둔 행도 저장한다.
    if current_rows:
        chunk_text = header + "\n" + "\n".join(current_rows)
        chunks.append(chunk_text)

    return chunks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="XML 문서 JSON을 검색용 청크 JSON으로 만듭니다.")
    parser.add_argument("--input-path", required=True, type=Path)
    parser.add_argument(
        "--output-path",
        type=Path,
        help="출력 경로 (생략 시 입력 파일 옆에 _xml_chunks_v2.json으로 저장)",
    )
    parser.add_argument("--chunk-size", type=int, default=600)
    parser.add_argument("--chunk-overlap", type=int, default=100)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.output_path is None:
        suffix = "_xml_documents.json"
        if not args.input_path.name.endswith(suffix):
            raise ValueError(
                "기본 출력 경로를 사용하려면 입력 파일명이 "
                "_xml_documents.json으로 끝나야 합니다. "
                "다른 이름은 --output-path를 지정하세요."
            )
        receipt = args.input_path.name.removesuffix(suffix)
        args.output_path = args.input_path.with_name(
            f"{receipt}_xml_chunks_v2.json"
        )
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
