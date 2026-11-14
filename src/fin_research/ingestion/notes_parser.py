# [OpenDART 연결재무제표 주석]
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.report_parser import build_locations, compact_text, normalize_xml_text
from fin_research.ingestion.table_parser import (
    TableParseError,
    extract_table_rows,
    find_by_xpath,
    raw_cell_text,
    validate_preservation,
)


NOTES_KEY = "consolidated_financial_statement_notes"
NOTE_HEADING_PATTERN = re.compile(r"^(\d+)\.\s+(.+)$")
SUBITEM_PATTERN = re.compile(r"^(?:[가-하]\.|\([0-9가-하]+\)|[0-9]+\))")


class NotesParseError(Exception):
    """연결재무제표 주석 구조 또는 원본 XML을 읽을 수 없을 때 발생한다."""


# 원본 출처 정보 생성]
def source_reference(filename: str, rcept_no: str, location: dict[str, Any]) -> dict[str, Any]:
    """주석·문단·표가 공통으로 사용하는 원본 출처 정보를 만든다."""
    return {
        "xml_filename": filename,
        "rcept_no": rcept_no,
        "xpath": location["xpath"],
        "element_index": location["element_index"],
    }

# JSON 문서 정보 XML 파일명으로 조회하기 위한 헬퍼
def document_by_filename(structure: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """구조 분석 JSON의 문서 정보를 XML 파일명으로 조회할 수 있게 만든다."""
    return {document["filename"]: document for document in structure.get("documents", [])}

# 연결감사보고서 주석과 사업보고서 비교용 주석 선택
def select_note_records(structure: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    연결감사보고서 주석 한 건과 사업보고서 비교용 주석을 선택

    연결감사보고서 주석은 한 건이어야 함
    두 건 이상이면 어느 문서를 써야 할지 임의로 고르지 않고 위치를 포함한 오류 발생시킴
    """
    documents = document_by_filename(structure)
    consolidated_records = []
    business_records = []
    for record in structure.get("consolidated_sections", []):
        if record.get("section_key") != NOTES_KEY:
            continue
        role = documents.get(record.get("source_xml"), {}).get("role")
        if role == "consolidated_audit_report":
            consolidated_records.append(record)
        elif role == "business_report":
            business_records.append(record)

    if len(consolidated_records) != 1:
        locations = ", ".join(
            f"{record.get('source_xml')} {record.get('location', {}).get('xpath')}"
            for record in consolidated_records
        )
        raise NotesParseError(
            "연결감사보고서 주석은 정확히 한 건이어야 합니다. "
            f"발견 건수: {len(consolidated_records)} ({locations})"
        )
    return consolidated_records[0], business_records

# 부모 요소 맵 생성
def parent_map(root: ElementTree.Element) -> dict[int, ElementTree.Element]:
    """요소의 부모를 찾기 위한 ID 기반 맵을 만든다."""
    return {id(child): parent for parent in root.iter() for child in parent}

# 주석 제목과 컨테이너 찾기
def find_notes_container(
    root: ElementTree.Element,
    note_record: dict[str, Any],
    locations: dict[int, dict[str, Any]],
) -> tuple[ElementTree.Element, ElementTree.Element, dict[str, Any]]:
    """구조 JSON의 주석 제목과 이를 직접 포함하는 XML 컨테이너를 확인한다."""
    heading = find_by_xpath(root, note_record["location"]["xpath"])
    parents = parent_map(root)
    container = parents.get(id(heading))
    if container is None:
        raise NotesParseError("주석 제목의 부모 컨테이너를 찾지 못했습니다.")

    actual_start = locations[id(heading)]["element_index"] + 1
    actual_end = max(locations[id(element)]["element_index"] for element in container.iter()) + 1
    evidence = note_record.get("evidence", {})
    expected_start = evidence.get("start_element_index")
    expected_end = evidence.get("end_element_index_exclusive")
    boundary = {
        "container_xpath": locations[id(container)]["xpath"],
        "heading_xpath": locations[id(heading)]["xpath"],
        "actual_start_element_index": actual_start,
        "actual_end_element_index_exclusive": actual_end,
        "expected_start_element_index": expected_start,
        "expected_end_element_index_exclusive": expected_end,
        # 구조 분석의 종료점은 다음 ATOC 제목이다. 그 사이에 컨테이너 밖 래퍼가 있을 수 있다.
        "container_within_structure_analysis_range": (
            actual_start == expected_start and actual_end <= expected_end
        ),
    }
    if not boundary["container_within_structure_analysis_range"]:
        raise NotesParseError("주석 XML 계층 경계가 구조 분석 JSON의 범위와 다릅니다.")
    return heading, container, boundary

# 주석 컨테이너에서 주석 제목 위치 찾기
def note_heading_positions(container: ElementTree.Element) -> list[tuple[int, int, str]]:
    """주석 컨테이너의 직접 자식 P에서 주석 번호·제목 위치를 찾는다."""
    positions = []
    for child_index, child in enumerate(container):
        if child.tag != "P":
            continue
        text = compact_text(child)
        match = NOTE_HEADING_PATTERN.match(text)
        if match:
            positions.append((child_index, int(match.group(1)), text))
    return positions

# 주석 내부 요소 종류 판별
def item_kind(element: ElementTree.Element, text: str) -> str:
    """주석 내부 요소를 하위 항목·문단·표·쪽나눔으로 구분한다."""
    if element.tag == "TABLE":
        return "table"
    if element.tag == "PGBRK":
        return "page_break"
    if element.tag == "P":
        if not text:
            return "empty_paragraph"
        if SUBITEM_PATTERN.match(compact_text(element)):
            return "subitem"
        return "paragraph"
    return "xml_element"

# 주석 내부 요소 추출
def extract_item(
    element: ElementTree.Element,
    filename: str,
    rcept_no: str,
    locations: dict[int, dict[str, Any]],
    sequence: int,
) -> dict[str, Any]:
    """문단 또는 표 하나를 원문 텍스트·병합 셀·출처와 함께 추출한다."""
    text = raw_cell_text(element)
    kind = item_kind(element, text)
    record: dict[str, Any] = {
        "sequence": sequence,
        "kind": kind,
        "source": source_reference(filename, rcept_no, locations[id(element)]),
    }
    if kind == "table":
        table = extract_table_rows(element)
        preservation = validate_preservation(element, table)
        if not (
            preservation["all_cell_texts_match_source"]
            and preservation["rowspan_colspan_match_source"]
            and table["layout_validation"]["no_overlapping_logical_columns"]
        ):
            raise NotesParseError(f"주석 표 원문 보존 검증에 실패했습니다: {record['source']['xpath']}")
        record["table"] = table
        record["raw_cell_preservation"] = preservation
    else:
        record["text"] = text
    return record

# 주석 컨테이너에서 번호별 주석과 머리말 추출
def reference_titles_by_number(
    business_titles: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """사업보고서의 연결 주석 제목을 번호별 후보 목록으로 정리한다.

    같은 번호에 서로 다른 제목이 둘 이상이면 제목과 본문의 경계를 임의로
    정하지 않도록 후보를 그대로 유지한다.
    """
    references: dict[int, list[dict[str, Any]]] = {}
    for title in business_titles:
        references.setdefault(title["number"], []).append(title)
    return references


def title_without_consolidated_marker(title: str, number: int) -> str | None:
    """사업보고서 제목에서 끝의 '(연결)' 표기만 제거한 비교용 제목을 만든다."""
    match = re.match(rf"^{number}\.\s+(.+)$", title)
    if not match:
        return None
    body = re.sub(r"\s*\(연결\)\s*$", "", match.group(1)).strip()
    return f"{number}. {body}" if body else None


def resolve_note_heading(
    heading: ElementTree.Element,
    number: int,
    references: dict[int, list[dict[str, Any]]],
    filename: str,
    rcept_no: str,
    locations: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """제목 P 안에 이어진 본문을 원문 간 제목으로만 안전하게 분리한다.

    연결감사보고서의 일부 주석은 제목과 첫 본문을 같은 P에 붙여 기록한다.
    이 경우 같은 접수번호의 사업보고서에 별도 기록된 '(연결)' 주석 제목이
    하나뿐이고 감사보고서 P의 맨 앞과 정확히 일치할 때에만 분리한다.
    조건을 만족하지 않으면 원문 전체를 보존하고 ``review_required``로 남긴다.
    """
    raw_text = raw_cell_text(heading)
    candidates = references.get(number, [])
    source = source_reference(filename, rcept_no, locations[id(heading)])
    boundary: dict[str, Any] = {
        "source_text": raw_text,
        "source_has_child_elements": bool(list(heading)),
        "reference_title_count": len(candidates),
        "status": "review_required",
    }

    if len(candidates) != 1:
        boundary["reason"] = "사업보고서의 대응 제목이 없거나 복수여서 경계를 확정할 수 없습니다."
        return {"title": None, "inline_body": None, "boundary": boundary}

    reference = candidates[0]
    reference_title = title_without_consolidated_marker(reference["title"], number)
    if reference_title is None or not raw_text.startswith(reference_title):
        boundary["reason"] = "사업보고서 제목이 연결감사보고서 제목 P의 시작과 일치하지 않습니다."
        boundary["reference_title"] = reference.get("title")
        return {"title": None, "inline_body": None, "boundary": boundary}

    title_end = len(reference_title)
    # 감사보고서 제목에만 붙은 콜론은 제목의 일부로 보존한다.
    if title_end < len(raw_text) and raw_text[title_end] in {":", "："}:
        title_end += 1
    title = raw_text[:title_end]
    inline_body = raw_text[title_end:]
    boundary.update(
        {
            "reference_title": reference["title"],
            "reference_source": reference["source"],
            "title_character_end": title_end,
        }
    )
    # 제목 뒤 공백만 남은 경우는 본문이 아니므로 별도 item을 만들지 않는다.
    if inline_body and not inline_body.strip():
        boundary["title_character_end"] = len(raw_text)
        boundary["status"] = "title_only"
        boundary["reason"] = "제목 P 뒤에는 공백만 있고 같은 요소 안의 본문 문자열이 없습니다."
        return {"title": raw_text, "inline_body": None, "boundary": boundary}
    if inline_body:
        boundary["status"] = "separated_with_business_report_reference"
        boundary["reason"] = "동일 접수번호 사업보고서의 연결 주석 제목과 원문 접두사가 일치합니다."
        return {"title": title, "inline_body": inline_body, "boundary": boundary}

    boundary["status"] = "title_only"
    boundary["reason"] = "제목 P 뒤에 같은 요소 안의 본문 문자열이 없습니다."
    return {"title": title, "inline_body": None, "boundary": boundary}


def extract_notes_from_container(
    container: ElementTree.Element,
    heading: ElementTree.Element,
    filename: str,
    rcept_no: str,
    locations: dict[int, dict[str, Any]],
    business_titles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """주석 컨테이너에서 번호별 주석과 번호 앞의 머리말을 원래 순서로 추출한다."""
    children = list(container)
    heading_index = children.index(heading)

    # 실제 주석 섹션 제목 이후에 등장하는 번호별 주석만 추출
    positions = [
        position
        for position in note_heading_positions(container)
        if position[0] > heading_index
    ]

    if not positions:
        raise NotesParseError(
            "주석 제목 이후에 번호가 있는 주석 항목을 찾지 못했습니다."
        )

    # 같은 번호의 주석이 중복 추출됐는지 확인
    note_numbers = [number for _, number, _ in positions]

    if len(note_numbers) != len(set(note_numbers)):
        raise NotesParseError(
            f"주석 번호가 중복되었습니다: {note_numbers}"
        )
    
    positions = note_heading_positions(container)
    if not positions:
        raise NotesParseError("주석 컨테이너에서 번호가 있는 주석 제목을 찾지 못했습니다.")

    preamble = [
        extract_item(child, filename, rcept_no, locations, sequence)
        for sequence, child in enumerate(children[heading_index + 1 : positions[0][0]])
    ]
    references = reference_titles_by_number(business_titles)
    notes = []
    for note_index, (child_index, number, title) in enumerate(positions):
        next_index = positions[note_index + 1][0] if note_index + 1 < len(positions) else len(children)
        heading_element = children[child_index]
        heading_resolution = resolve_note_heading(
            heading_element,
            number,
            references,
            filename,
            rcept_no,
            locations,
        )
        items = [
            extract_item(child, filename, rcept_no, locations, sequence)
            for sequence, child in enumerate(children[child_index + 1 : next_index], start=bool(heading_resolution["inline_body"]))
        ]
        if heading_resolution["inline_body"]:
            items.insert(
                0,
                {
                    "sequence": 0,
                    "kind": item_kind(heading_element, heading_resolution["inline_body"]),
                    "source": source_reference(filename, rcept_no, locations[id(heading_element)]),
                    "text": heading_resolution["inline_body"],
                    "source_fragment": {
                        "kind": "same_element_suffix",
                        "start_character_index": heading_resolution["boundary"]["title_character_end"],
                        "end_character_index_exclusive": len(heading_resolution["boundary"]["source_text"]),
                    },
                },
            )
        notes.append(
            {
                "number": number,
                "title": heading_resolution["title"],
                "heading_raw_text": heading_resolution["boundary"]["source_text"],
                "title_boundary": heading_resolution["boundary"],
                "source": source_reference(filename, rcept_no, locations[id(heading_element)]),
                "items": items,
                "summary": {
                    "paragraph_count": sum(item["kind"] in {"paragraph", "subitem"} for item in items),
                    "table_count": sum(item["kind"] == "table" for item in items),
                    "subitem_count": sum(item["kind"] == "subitem" for item in items),
                },
            }
        )
    return preamble, notes

# 주석 범위 내 구조 제목 추출
def numbered_titles_in_section(
    root: ElementTree.Element,
    note_record: dict[str, Any],
    filename: str,
    rcept_no: str,
) -> list[dict[str, Any]]:
    """사업보고서 주석 범위의 구조 제목을 비교용 번호·제목 목록으로 변환한다."""
    ordered, locations = build_locations(root)
    evidence = note_record.get("evidence", {})
    start = evidence.get("start_element_index")
    end = evidence.get("end_element_index_exclusive")
    titles = []
    for element in ordered:
        location = locations[id(element)]
        if not (start <= location["element_index"] < end):
            continue
        if element.attrib.get("ATOC") != "Y":
            continue
        text = compact_text(element)
        match = NOTE_HEADING_PATTERN.match(text)
        if match:
            titles.append(
                {
                    "number": int(match.group(1)),
                    "title": text,
                    "source": source_reference(filename, rcept_no, location),
                }
            )
    return titles


def validate_extraction_coverage(
    container: ElementTree.Element,
    heading: ElementTree.Element,
    preamble: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    locations: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """주석 컨테이너의 직접 자식과 추출 결과를 대조해 누락·순서 변경을 찾는다.

    제목과 첫 본문이 하나의 P에 붙은 경우에는 그 P를 제목의 출처로 한 번만
    계산한다. 표는 preamble과 주석 items를 합쳐 원본 TABLE 목록과 별도로 대조한다.
    """
    children = list(container)
    heading_index = children.index(heading)
    source_children = children[heading_index + 1 :]
    source_paths = [locations[id(child)]["xpath"] for child in source_children]

    extracted_paths = [item["source"]["xpath"] for item in preamble]
    for note in notes:
        note_path = note["source"]["xpath"]
        extracted_paths.append(note_path)
        extracted_paths.extend(
            item["source"]["xpath"]
            for item in note["items"]
            if item["source"]["xpath"] != note_path
        )

    source_table_paths = [
        locations[id(child)]["xpath"] for child in source_children if child.tag == "TABLE"
    ]
    extracted_table_paths = [
        item["source"]["xpath"] for item in preamble if item["kind"] == "table"
    ]
    extracted_table_paths.extend(
        item["source"]["xpath"]
        for note in notes
        for item in note["items"]
        if item["kind"] == "table"
    )

    missing_tables = [path for path in source_table_paths if path not in extracted_table_paths]
    unexpected_tables = [path for path in extracted_table_paths if path not in source_table_paths]
    missing_elements = [path for path in source_paths if path not in extracted_paths]
    unexpected_elements = [path for path in extracted_paths if path not in source_paths]
    validation = {
        "source_direct_element_count": len(source_paths),
        "extracted_direct_element_count": len(extracted_paths),
        "source_table_count": len(source_table_paths),
        "preamble_table_count": sum(item["kind"] == "table" for item in preamble),
        "notes_table_count": sum(note["summary"]["table_count"] for note in notes),
        "extracted_table_count": len(extracted_table_paths),
        "missing_table_xpaths": missing_tables,
        "unexpected_table_xpaths": unexpected_tables,
        "missing_element_xpaths": missing_elements,
        "unexpected_element_xpaths": unexpected_elements,
        "source_order_preserved": source_paths == extracted_paths,
    }
    validation["all_source_tables_extracted"] = not missing_tables and not unexpected_tables
    validation["all_direct_elements_represented"] = not missing_elements and not unexpected_elements
    if not (
        validation["all_source_tables_extracted"]
        and validation["all_direct_elements_represented"]
        and validation["source_order_preserved"]
    ):
        raise NotesParseError("주석 원문 요소·표의 출처 또는 순서 보존 검증에 실패했습니다.")
    return validation

# 중복 주석 비교
def compare_duplicate_notes(
    consolidated_notes: list[dict[str, Any]],
    business_titles: list[dict[str, Any]],
) -> dict[str, Any]:
    """두 문서의 번호별 주석을 비교하되 본문을 병합하거나 삭제하지 않는다."""
    consolidated_by_number = {note["number"]: note["title"] for note in consolidated_notes}
    business_by_number = {note["number"]: note["title"] for note in business_titles}
    shared_numbers = sorted(set(consolidated_by_number) & set(business_by_number))
    return {
        "comparison_method": "주석 번호와 원문 제목 비교",
        "content_merge_performed": False,
        "same_note_numbers": shared_numbers,
        "only_in_consolidated_audit_report": sorted(set(consolidated_by_number) - set(business_by_number)),
        "only_in_business_report": sorted(set(business_by_number) - set(consolidated_by_number)),
        "title_pairs": [
            {
                "number": number,
                "consolidated_audit_title": consolidated_by_number[number],
                "business_report_title": business_by_number[number],
                "exact_title_match": consolidated_by_number[number] == business_by_number[number],
            }
            for number in shared_numbers
        ],
    }

# 연결감사보고서 주석 추출
def extract_consolidated_notes(zip_path: Path, structure_path: Path) -> dict[str, Any]:
    """구조 분석 JSON을 근거로 연결감사보고서 주석의 실제 본문을 추출한다."""
    if not zip_path.is_file() or not structure_path.is_file():
        raise NotesParseError("원본 ZIP 또는 구조 분석 JSON을 찾지 못했습니다.")
    try:
        structure = json.loads(structure_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NotesParseError(f"구조 분석 JSON을 읽지 못했습니다: {error}") from error

    consolidated_record, business_records = select_note_records(structure)
    rcept_no = structure["source"]["rcept_no"]
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as error:
        raise NotesParseError("유효하지 않은 원본 ZIP입니다.") from error

    with archive:
        def read_root(filename: str) -> ElementTree.Element:
            try:
                return ElementTree.fromstring(normalize_xml_text(archive.read(filename).decode("utf-8"))[0])
            except KeyError as error:
                raise NotesParseError(f"ZIP에 XML 파일이 없습니다: {filename}") from error

        consolidated_root = read_root(consolidated_record["source_xml"])
        ordered, locations = build_locations(consolidated_root)
        heading, container, boundary = find_notes_container(consolidated_root, consolidated_record, locations)

        business_titles = []
        for business_record in business_records:
            business_root = read_root(business_record["source_xml"])
            business_titles.extend(
                numbered_titles_in_section(
                    business_root,
                    business_record,
                    business_record["source_xml"],
                    rcept_no,
                )
            )

        preamble, notes = extract_notes_from_container(
            container,
            heading,
            consolidated_record["source_xml"],
            rcept_no,
            locations,
            business_titles,
        )
        coverage = validate_extraction_coverage(container, heading, preamble, notes, locations)

    note_numbers = [note["number"] for note in notes]
    expected_numbers = list(range(1, max(note_numbers) + 1)) if note_numbers else []
    missing_numbers = sorted(set(expected_numbers) - set(note_numbers))
    if missing_numbers:
        raise NotesParseError(f"주석 번호가 누락되었습니다: {missing_numbers}")

    return {
        "source": {
            **structure["source"],
            "structure_analysis_path": str(structure_path),
            "extraction_scope": "consolidated_financial_statement_notes_only",
        },
        "notes_source": {
            "xml_filename": consolidated_record["source_xml"],
            "rcept_no": rcept_no,
            "note_heading": source_reference(
                consolidated_record["source_xml"], rcept_no, consolidated_record["location"]
            ),
        },
        "boundary_validation": boundary,
        "coverage_validation": coverage,
        "preamble": preamble,
        "notes": notes,
        "summary": {
            "note_count": len(notes),
            "paragraph_count": sum(note["summary"]["paragraph_count"] for note in notes),
            "table_count": sum(note["summary"]["table_count"] for note in notes),
            "preamble_item_count": len(preamble),
            "preamble_table_count": coverage["preamble_table_count"],
            "total_table_count_including_preamble": coverage["extracted_table_count"],
            "missing_note_numbers": missing_numbers,
        },
        "duplicate_comparison": compare_duplicate_notes(notes, business_titles),
    }

# 기본 출력 경로 생성
def default_output_path(result: dict[str, Any]) -> Path:
    """기업·사업연도·접수번호에 따른 주석 JSON 경로를 만든다."""
    source = result["source"]
    return (
        PROJECT_ROOT / "data" / "processed" / "dart" / source["company_code"]
        / str(source["fiscal_year"]) / f"{source['rcept_no']}_consolidated_notes.json"
    )

# 추출 결과 저장
def save_extraction(result: dict[str, Any], output_path: Path) -> Path:
    """추출한 주석 결과를 JSON으로 저장한다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path

# 명령줄 인터페이스
def main() -> int:
    """명령줄에서 연결재무제표 주석 JSON을 생성한다."""
    parser = argparse.ArgumentParser(description="OpenDART 연결재무제표 주석 추출")
    parser.add_argument("--zip-path", required=True, type=Path)
    parser.add_argument("--structure-path", required=True, type=Path)
    parser.add_argument("--output-path", type=Path)
    args = parser.parse_args()
    try:
        result = extract_consolidated_notes(args.zip_path, args.structure_path)
    except (NotesParseError, TableParseError) as error:
        parser.error(str(error))
    output_path = args.output_path or default_output_path(result)
    save_extraction(result, output_path)
    print(f"주석 추출 결과: {output_path}")
    print(f"주석 수: {result['summary']['note_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
