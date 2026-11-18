"""DART 감사보고서 XML을 검색용 문서 목록으로 정리한다.

원본 ZIP과 XML은 수정하지 않는다. ``separate``는 감사보고서, ``consolidated``는
연결감사보고서만 선택해 사업보고서 본문의 비정상 XML 표기와 분리한다.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from fin_research.ingestion.report_parser import (
    build_locations,
    compact_text,
    normalize_xml_text,
    normalized_label,
)
from fin_research.ingestion.table_parser import raw_cell_text


SCOPE_DOCUMENT_NAME = {
    "separate": "감사보고서",
    "consolidated": "연결감사보고서",
}

SCOPE_SECTIONS = {
    "separate": ("재무제표", "재무제표 주석"),
    "consolidated": ("연결재무제표", "연결재무제표 주석"),
}

NOTE_HEADING = re.compile(r"^\d+\.\s+.+")


class XmlDocumentError(Exception):
    """검색용 XML 문서를 만들 수 없을 때 발생한다."""


def _clean(text: str) -> str:
    """셀의 숫자·괄호는 유지하고 불필요한 공백만 정리한다."""

    return " ".join(text.split())


def _table_text(table: ElementTree.Element) -> str:
    """표 행과 셀의 순서를 보존한 검색용 텍스트를 만든다."""

    rows: list[str] = []
    for row in table.iter("TR"):
        cells = [
            _clean(raw_cell_text(cell))
            for cell in row
            if cell.tag in {"TD", "TH", "TU"} and _clean(raw_cell_text(cell))
        ]
        if cells:
            rows.append(" | ".join(cells))
    if rows:
        return "\n".join(rows)
    return _clean(raw_cell_text(table))


def element_text(element: ElementTree.Element) -> str:
    """문단·표·기타 XML 요소를 검색 가능한 텍스트로 바꾼다."""

    if element.tag == "TABLE":
        return _table_text(element)
    return _clean(raw_cell_text(element))


def _statement_kind(text: str) -> str | None:
    compact = normalized_label(text)
    if "재무상태표" in compact:
        return "재무상태표"
    if "포괄손익계산서" in compact:
        return "포괄손익계산서"
    if "손익계산서" in compact:
        return "손익계산서"
    if "자본변동표" in compact:
        return "자본변동표"
    if "현금흐름표" in compact:
        return "현금흐름표"
    return None


def _read_scope_xml(
    zip_path: Path, scope: str
) -> tuple[str, ElementTree.Element, dict[str, int]]:
    """ZIP 안에서 scope에 맞는 감사보고서 XML 하나를 찾아 메모리에서만 읽는다."""

    expected_name = SCOPE_DOCUMENT_NAME[scope]
    available: list[str] = []
    parse_notes: list[str] = []

    with zipfile.ZipFile(zip_path) as archive:
        for filename in archive.namelist():
            if not filename.lower().endswith(".xml"):
                continue
            try:
                source = archive.read(filename).decode("utf-8")
                prepared, normalization = normalize_xml_text(source)
                root = ElementTree.fromstring(prepared)
            except (UnicodeDecodeError, ElementTree.ParseError) as error:
                # 사업보고서 본문의 비정상 표기는 선택할 감사보고서 판단을 막지 않는다.
                parse_notes.append(f"{filename}: {error}")
                continue

            document_name = root.find("DOCUMENT-NAME")
            title = compact_text(document_name) if document_name is not None else ""
            if title:
                available.append(title)
            if normalized_label(title) == expected_name:
                return filename, root, normalization

    detail = ", ".join(available) or "없음"
    skipped = f"; 읽지 못한 XML: {', '.join(parse_notes)}" if parse_notes else ""
    raise XmlDocumentError(
        f"{scope} 기준의 '{expected_name}' XML을 찾지 못했습니다. 발견 문서: {detail}{skipped}"
    )


def _financial_section(root: ElementTree.Element) -> tuple[ElementTree.Element, ElementTree.Element]:
    """'(첨부) 재무제표' SECTION-1과 그 안의 주석 SECTION-2를 찾는다."""

    for section in root.iter("SECTION-1"):
        title = next(
            (
                child for child in section
                if child.tag == "TITLE"
                and "첨부" in normalized_label(compact_text(child))
                and "재무제표" in normalized_label(compact_text(child))
            ),
            None,
        )
        if title is None:
            continue
        notes = next(
            (
                child for child in section
                if child.tag == "SECTION-2"
                and (note_title := child.find("TITLE")) is not None
                and normalized_label(compact_text(note_title)) == "주석"
            ),
            None,
        )
        if notes is not None:
            return section, notes
    raise XmlDocumentError("'(첨부) 재무제표'와 그 주석 SECTION을 찾지 못했습니다.")


def _record(
    *,
    sequence: int,
    corp_code: str,
    fiscal_year: int,
    scope: str,
    company: str,
    filename: str,
    element: ElementTree.Element,
    locations: dict[int, dict[str, Any]],
    section: str,
    document_type: str,
    title: str,
    text: str,
) -> dict[str, Any]:
    """XML 위치를 보존한 검색용 문서 한 건을 만든다."""

    location = locations[id(element)]
    return {
        # 기존 청커의 문서 식별 방식과 맞추기 위해 page_id를 사용한다.
        "page_id": f"{corp_code}_{fiscal_year}_{scope}_xml_{sequence:04d}",
        "text": text,
        "company": company,
        "fiscal_year": fiscal_year,
        "statement_scope": scope,
        "document_type": document_type,
        "source_format": "xml",
        "source_file": filename,
        "source_xpath": location["xpath"],
        "source_element_index": location["element_index"],
        "section": section,
        "title": title,
    }


def build_xml_documents(
    zip_path: Path,
    corp_code: str,
    fiscal_year: int,
    scope: str,
    company: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """별도 또는 연결 감사보고서에서 재무제표 표와 번호별 주석을 추출한다."""

    if scope not in SCOPE_DOCUMENT_NAME:
        raise ValueError("scope는 separate 또는 consolidated여야 합니다.")
    if not zip_path.is_file():
        raise XmlDocumentError(f"원본 ZIP을 찾지 못했습니다: {zip_path}")

    filename, root, normalization = _read_scope_xml(zip_path, scope)
    _, locations = build_locations(root)
    financial_section, notes_section = _financial_section(root)
    statements_label, notes_label = SCOPE_SECTIONS[scope]
    records: list[dict[str, Any]] = []
    sequence = 0

    # 주석 SECTION-2 전의 TABLE을 재무제표 제목별로 묶는다.
    section_children = list(financial_section)
    notes_index = section_children.index(notes_section)
    statement_children = section_children[:notes_index]
    starts = [
        (index, _statement_kind(element_text(element)))
        for index, element in enumerate(statement_children)
        if element.tag == "TABLE" and _statement_kind(element_text(element))
    ]
    for position, (start, title) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(statement_children)
        parts = [element_text(element) for element in statement_children[start:end]]
        text = "\n".join(part for part in parts if part)
        if not text:
            continue
        sequence += 1
        records.append(
            _record(
                sequence=sequence, corp_code=corp_code, fiscal_year=fiscal_year,
                scope=scope, company=company, filename=filename,
                element=statement_children[start], locations=locations,
                section=statements_label, document_type="xml_financial_statement",
                title=title, text=text,
            )
        )

    # 주석은 '1. 회사의 개요'처럼 번호가 붙은 직접 자식 P를 경계로 묶는다.
    note_children = list(notes_section)
    note_starts = [
        (index, compact_text(element))
        for index, element in enumerate(note_children)
        if element.tag == "P" and NOTE_HEADING.match(compact_text(element))
    ]
    for position, (start, title) in enumerate(note_starts):
        end = note_starts[position + 1][0] if position + 1 < len(note_starts) else len(note_children)
        parts = [element_text(element) for element in note_children[start:end]]
        text = "\n".join(part for part in parts if part)
        if not text:
            continue
        sequence += 1
        records.append(
            _record(
                sequence=sequence, corp_code=corp_code, fiscal_year=fiscal_year,
                scope=scope, company=company, filename=filename,
                element=note_children[start], locations=locations,
                section=notes_label, document_type="xml_note",
                title=title, text=text,
            )
        )

    if not records:
        raise XmlDocumentError("검색용으로 추출된 재무제표 표·주석이 없습니다.")

    metadata = {
        "source_format": "xml",
        "source_xml": filename,
        "statement_scope": scope,
        "normalization": normalization,
        "financial_statement_documents": sum(
            item["document_type"] == "xml_financial_statement" for item in records
        ),
        "note_documents": sum(item["document_type"] == "xml_note" for item in records),
    }
    return records, metadata
