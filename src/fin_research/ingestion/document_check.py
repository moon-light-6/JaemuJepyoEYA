# [XML 원문·추출 결과 재점검하는 도구]

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from fin_research.ingestion.report_parser import (
    CONSOLIDATED_STATEMENTS,
    build_locations,
    compact_text,
    normalize_xml_text,
)


class DocumentCheckError(Exception):
    """원문 또는 기존 추출 JSON을 점검할 수 없을 때 발생한다."""


def read_json(path: Path) -> dict[str, Any]:
    """UTF-8 JSON을 읽고 객체 형식인지 확인한다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DocumentCheckError(f"점검 JSON을 읽지 못했습니다: {path} ({error})") from error
    if not isinstance(data, dict):
        raise DocumentCheckError(f"점검 JSON의 최상위 값은 객체여야 합니다: {path}")
    return data


def parent_map(root: ElementTree.Element) -> dict[int, ElementTree.Element]:
    """제목 요소에서 실제 본문 컨테이너로 이동하기 위한 부모 맵을 만든다."""
    return {id(child): parent for parent in root.iter() for child in parent}


def find_single_topic_heading(
    root: ElementTree.Element,
    phrase: str,
    required_tag: str,
    starts_with: bool,
) -> ElementTree.Element:
    """태그·문구 조건을 모두 만족하는 실제 제목 요소 하나만 선택한다."""
    matches = []
    for element in root.iter(required_tag):
        text = compact_text(element)
        matched = text.startswith(phrase) if starts_with else phrase in text
        if matched:
            matches.append(element)
    if len(matches) != 1:
        raise DocumentCheckError(
            f"'{phrase}' 제목 요소는 하나여야 합니다. 발견 개수: {len(matches)}"
        )
    return matches[0]


def topic_evidence(
    heading: ElementTree.Element,
    root: ElementTree.Element,
    locations: dict[int, dict[str, Any]],
    filename: str,
    rcept_no: str,
) -> dict[str, Any]:
    """제목과 같은 XML 계층의 실제 문단·표를 세어 본문 존재를 확인한다."""
    container = parent_map(root).get(id(heading))
    if container is None:
        raise DocumentCheckError("제목의 본문 컨테이너를 찾지 못했습니다.")
    heading_text = compact_text(heading)
    container_text = compact_text(container)
    descendants_after_heading = [
        element
        for element in container.iter()
        if locations[id(element)]["element_index"] > locations[id(heading)]["element_index"]
    ]
    paragraphs = [
        element for element in descendants_after_heading if element.tag == "P" and compact_text(element)
    ]
    tables = [element for element in descendants_after_heading if element.tag == "TABLE"]
    return {
        "present": True,
        "source": {
            "format": "xml",
            "xml_filename": filename,
            "rcept_no": rcept_no,
            "xpath": locations[id(heading)]["xpath"],
            "element_index": locations[id(heading)]["element_index"],
        },
        "heading_text": heading_text,
        "body_container_xpath": locations[id(container)]["xpath"],
        "following_nonempty_paragraph_count": len(paragraphs),
        "following_table_count": len(tables),
        "container_text_longer_than_heading": len(container_text) > len(heading_text),
        "has_substantive_body": bool(paragraphs or tables) and len(container_text) > len(heading_text),
    }


def inspect_business_report_topics(zip_path: Path, rcept_no: str) -> dict[str, Any]:
    """사업보고서 XML에서 사업 개요와 회계감사 의견의 실제 본문 위치를 확인한다."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            filename = next(name for name in archive.namelist() if name.endswith(".xml") and "_" not in name)
            xml_text = archive.read(filename).decode("utf-8")
    except (OSError, zipfile.BadZipFile, StopIteration, UnicodeDecodeError) as error:
        raise DocumentCheckError(f"사업보고서 XML을 읽지 못했습니다: {error}") from error

    root = ElementTree.fromstring(normalize_xml_text(xml_text)[0])
    _, locations = build_locations(root)
    business_overview = find_single_topic_heading(root, "사업의 개요", "TITLE", starts_with=False)
    audit_opinion = find_single_topic_heading(
        root, "가. 회계감사인의 감사의견 등", "P", starts_with=True
    )
    return {
        "business_overview": topic_evidence(
            business_overview, root, locations, filename, rcept_no
        ),
        "audit_opinion": topic_evidence(audit_opinion, root, locations, filename, rcept_no),
    }


def inspect_xml_scope(
    zip_path: Path,
    structure_path: Path,
    tables_path: Path,
    notes_path: Path,
) -> dict[str, Any]:
    """기존 XML 구조·표·주석 결과 대조해서 기록"""
    structure = read_json(structure_path)
    tables = read_json(tables_path)
    notes = read_json(notes_path)
    source = structure["source"]

    # 구조 분석, 재무제표, 주석이 동일한 공시 자료인지 확인
    for name, data in [("tables", tables), ("notes", notes)]:
        for key in ("company_code", "fiscal_year", "rcept_no"):
            expected = source.get(key)
            actual = data.get("source", {}).get(key)

            if expected is None or actual is None or str(expected) != str(actual):
                raise DocumentCheckError(
                    f"{name}의 {key}가 구조 분석 자료와 일치하지 않습니다. "
                    f"기준: {expected}, 입력: {actual}"
                )

            
    fiscal_year = int(source["fiscal_year"])
    previous_year = fiscal_year - 1
    expected_statement_keys = set(CONSOLIDATED_STATEMENTS)
    extracted_tables = tables.get("tables", [])
    extracted_keys = [table.get("section_key") for table in extracted_tables]
    statement_checks = []
    for table in extracted_tables:
        periods = table.get("periods", [])
        mapping = table.get("period_column_mapping", [])
        statement_checks.append(
            {
                "section_key": table.get("section_key"),
                "title": table.get("title"),
                "source": table.get("source"),
                "has_current_year": any(str(fiscal_year) in period for period in periods),
                "has_previous_year": any(str(previous_year) in period for period in periods),
                "period_mapping_verified": bool(mapping) and all(
                    item.get("matched_title_period") and item.get("has_amount_cells")
                    for item in mapping
                ),
                "raw_cell_preservation_verified": table.get("validation", {}).get(
                    "all_cell_texts_match_source"
                )
                and table.get("validation", {}).get("rowspan_colspan_match_source"),
            }
        )

    review_required = [
        {
            "note_number": note["number"],
            "reason": note["title_boundary"].get("reason"),
            "source": note["source"],
        }
        for note in notes.get("notes", [])
        if note.get("title_boundary", {}).get("status") == "review_required"
    ]
    return {
        "source": source,
        "format_validation": {
            "format": "xml",
            "source_locator": "xpath",
            "pdf_comparison_performed": False,
        },
        "consolidated_financial_statements": {
            "expected_count": len(expected_statement_keys),
            "extracted_count": len(extracted_tables),
            "missing_section_keys": sorted(expected_statement_keys - set(extracted_keys)),
            "duplicate_section_keys": sorted(
                {key for key in extracted_keys if extracted_keys.count(key) > 1}
            ),
            "statements": statement_checks,
        },
        "consolidated_notes": {
            "extracted_note_count": notes.get("summary", {}).get("note_count"),
            "extracted_note_numbers": [note["number"] for note in notes.get("notes", [])],
            "missing_note_numbers": notes.get("summary", {}).get("missing_note_numbers", []),
            "review_required": review_required,
            "source_table_count": notes.get("coverage_validation", {}).get("source_table_count"),
            "extracted_table_count": notes.get("coverage_validation", {}).get("extracted_table_count"),
            "all_source_tables_extracted": notes.get("coverage_validation", {}).get(
                "all_source_tables_extracted"
            ),
        },
        "business_report_topics": inspect_business_report_topics(zip_path, source["rcept_no"]),
    }


def save_document_check(result: dict[str, Any], output_path: Path) -> Path:
    """원문 범위와 문서 점검 결과를 팀 인수인계용 JSON으로 저장한다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path
