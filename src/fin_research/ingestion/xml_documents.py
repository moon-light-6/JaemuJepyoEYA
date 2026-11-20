"""DART 감사보고서 XML을 검색용 문서 목록으로 정리한다.

원본 ZIP과 XML은 수정하지 않는다. ``separate``는 감사보고서, ``consolidated``는
연결감사보고서를 우선 선택한다.

감사보고서 첨부가 없는 공시(예: 본문만 다시 제출한 기재정정 사업보고서)는
사업보고서 본문의 'III. 재무에 관한 사항 > (연결)재무제표 / (연결)재무제표 주석'에서
같은 형식의 문서를 만든다. 본문은 TABLE-GROUP 하나가 재무제표 하나 또는 주석 하나다.
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

# 사업보고서 본문에서 따옴표가 깨진 속성. 예: ENG="" KB Insurance Co., Ltd ""
BROKEN_ATTRIBUTE = re.compile(r'(\s[A-Za-z_][\w.-]*)=""([^"<>]*)""')
# 'III. 재무에 관한 사항' 아래 본문 SECTION-2 제목 (번호 제외)
BUSINESS_REPORT_SECTIONS = {
    "separate": ("재무제표", "재무제표주석"),
    "consolidated": ("연결재무제표", "연결재무제표주석"),
}
BUSINESS_REPORT_NAME = "사업보고서"
_LEADING_NUMBER = re.compile(r"^[\dIVX]+[.\-]?\s*")


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
            # TE: 사업보고서 본문 재무제표의 XBRL 값 셀 (감사보고서는 TD·TU)
            if cell.tag in {"TD", "TH", "TU", "TE"} and _clean(raw_cell_text(cell))
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


def repair_broken_attributes(xml_text: str) -> tuple[str, int]:
    """속성값 따옴표가 깨진 표기(name="" 값 "")를 name=" 값 "로 메모리에서만 고친다."""

    return BROKEN_ATTRIBUTE.subn(r'\1="\2"', xml_text)


def _read_scope_xml(
    zip_path: Path, scope: str
) -> tuple[str, ElementTree.Element, dict[str, int], str]:
    """ZIP 안에서 scope에 맞는 감사보고서 XML 하나를 찾아 메모리에서만 읽는다.

    감사보고서가 없고 사업보고서 본문만 있으면 본문을 대신 돌려준다.
    반환: (파일명, 루트, 정규화 통계, 원천 문서 종류 '감사보고서' | '사업보고서')
    """

    expected_name = SCOPE_DOCUMENT_NAME[scope]
    available: list[str] = []
    parse_notes: list[str] = []
    business_report: tuple[str, ElementTree.Element, dict[str, int]] | None = None

    with zipfile.ZipFile(zip_path) as archive:
        for filename in archive.namelist():
            if not filename.lower().endswith(".xml"):
                continue
            try:
                source = archive.read(filename).decode("utf-8")
                prepared, normalization = normalize_xml_text(source)
                prepared, repaired = repair_broken_attributes(prepared)
                normalization = {**normalization, "repaired_broken_attributes": repaired}
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
                return filename, root, normalization, "감사보고서"
            if normalized_label(title) == BUSINESS_REPORT_NAME and business_report is None:
                business_report = (filename, root, normalization)

    if business_report is not None:
        return (*business_report, "사업보고서")

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


def _business_report_sections(
    root: ElementTree.Element, scope: str
) -> tuple[ElementTree.Element, ElementTree.Element]:
    """사업보고서 본문에서 '(연결)재무제표'와 '(연결)재무제표 주석' SECTION-2를 찾는다."""

    statements_name, notes_name = BUSINESS_REPORT_SECTIONS[scope]
    found: dict[str, ElementTree.Element] = {}
    for section in root.iter("SECTION-2"):
        title = section.find("TITLE")
        if title is None:
            continue
        label = normalized_label(_LEADING_NUMBER.sub("", compact_text(title)))
        if label in (statements_name, notes_name) and label not in found:
            found[label] = section
    missing = [name for name in (statements_name, notes_name) if name not in found]
    if missing:
        raise XmlDocumentError(f"사업보고서 본문에서 {missing} 절을 찾지 못했습니다.")
    return found[statements_name], found[notes_name]


def _table_groups(section: ElementTree.Element) -> list[ElementTree.Element]:
    return [child for child in section if child.tag == "TABLE-GROUP"]


def _group_title(group: ElementTree.Element) -> str:
    title = group.find("TITLE")
    return compact_text(title) if title is not None else ""


def _group_text(group: ElementTree.Element) -> str:
    """TABLE-GROUP의 제목·표·문단을 순서대로 이어 붙인다 (표는 행 구조 유지)."""

    parts = [element_text(child) for child in group]
    return "\n".join(part for part in parts if part)


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
    """별도 또는 연결 감사보고서에서 재무제표 표와 번호별 주석을 추출한다.

    감사보고서 첨부가 없으면 사업보고서 본문의 (연결)재무제표·주석 절에서 추출하고,
    metadata["source_document"]에 '사업보고서'로 기록한다.
    """

    if scope not in SCOPE_DOCUMENT_NAME:
        raise ValueError("scope는 separate 또는 consolidated여야 합니다.")
    if not zip_path.is_file():
        raise XmlDocumentError(f"원본 ZIP을 찾지 못했습니다: {zip_path}")

    filename, root, normalization, source_document = _read_scope_xml(zip_path, scope)
    _, locations = build_locations(root)
    statements_label, notes_label = SCOPE_SECTIONS[scope]
    records: list[dict[str, Any]] = []
    sequence = 0

    def add(element: ElementTree.Element, section: str, document_type: str, title: str, text: str) -> None:
        nonlocal sequence
        if not text:
            return
        sequence += 1
        records.append(
            _record(
                sequence=sequence, corp_code=corp_code, fiscal_year=fiscal_year,
                scope=scope, company=company, filename=filename,
                element=element, locations=locations,
                section=section, document_type=document_type,
                title=title, text=text,
            )
        )

    if source_document == "사업보고서":
        # 본문: TABLE-GROUP 하나 = 재무제표 하나 또는 주석 하나
        statements_section, notes_section = _business_report_sections(root, scope)
        for group in _table_groups(statements_section):
            heading = _group_title(group)
            add(group, statements_label, "xml_financial_statement",
                _statement_kind(heading) or heading, _group_text(group))
        for group in _table_groups(notes_section):
            add(group, notes_label, "xml_note", _group_title(group), _group_text(group))
    else:
        financial_section, notes_section = _financial_section(root)
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
        # 감사보고서가 없어 사업보고서 본문에서 추출했는지 기록 (정답 위치 XPath 형식이 다름)
        "source_document": source_document,
        "statement_scope": scope,
        "normalization": normalization,
        "financial_statement_documents": sum(
            item["document_type"] == "xml_financial_statement" for item in records
        ),
        "note_documents": sum(item["document_type"] == "xml_note" for item in records),
    }
    return records, metadata
