# [OpenDART 연결재무제표 표 추출기]

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

from fin_research.ingestion.report_parser import (
    CONSOLIDATED_STATEMENTS,
    normalize_xml_text,
)

PERIOD_PATTERN = re.compile(r"\d{4}년")
PERIOD_KEY_PATTERN = re.compile(r"제\s*(\d+).*?기")
YEAR_PATTERN = re.compile(r"(\d{4})년")
DATE_ROW_YEAR_PATTERN = re.compile(r"\b(20\d{2})\.")
AMOUNT_CELL_PATTERN = re.compile(
    r"(?:-|\(?-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?)"
)


class TableParseError(Exception):
    """구조 분석 결과 또는 원본 XML 표를 읽을 수 없을 때 발생한다."""


def raw_cell_text(cell: ElementTree.Element) -> str:
    """XML 셀의 원문 텍스트를 숫자 변환이나 공백 정규화 없이 반환한다."""
    return "".join(cell.itertext())


def parse_span(cell: ElementTree.Element, attribute_name: str) -> int:
    """ROWSPAN·COLSPAN의 정수값을 읽고, 없거나 잘못된 값은 1로 처리한다."""
    value = cell.attrib.get(attribute_name, "1")
    try:
        return max(int(value), 1)
    except ValueError:
        return 1


def extract_table_rows(table: ElementTree.Element) -> dict[str, Any]:
    """TABLE의 행·셀 순서와 병합 속성을 보존해 JSON 친화적인 형태로 만든다.

    ``logical_column_start``는 ROWSPAN으로 점유된 열을 건너뛴 논리 위치다.
    셀의 ``text``와 ``attributes``는 원본 XML 값을 바꾸지 않고 기록한다.
    """
    rows: list[dict[str, Any]] = []
    active_rowspans: dict[int, int] = {}
    logical_column_count = 0
    layout_errors: list[str] = []

    for row_index, row in enumerate(table.iter("TR")):
        cells: list[dict[str, Any]] = []
        column = 0
        new_rowspans: dict[int, int] = {}
        for source_column_index, cell in enumerate(row):
            if cell.tag not in {"TD", "TH", "TU"}:
                continue
            while any(
                occupied_column in active_rowspans
                for occupied_column in range(column, column + parse_span(cell, "COLSPAN"))
            ):
                column += 1

            rowspan = parse_span(cell, "ROWSPAN")
            colspan = parse_span(cell, "COLSPAN")
            cells.append(
                {
                    "source_column_index": source_column_index,
                    "logical_column_start": column,
                    "tag": cell.tag,
                    "text": raw_cell_text(cell),
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "attributes": dict(cell.attrib),
                }
            )
            if rowspan > 1:
                for occupied_column in range(column, column + colspan):
                    if occupied_column in new_rowspans:
                        layout_errors.append(
                            f"행 {row_index}의 논리 열 {occupied_column}에 ROWSPAN이 중복됩니다."
                        )
                    new_rowspans[occupied_column] = rowspan - 1
            column += colspan

        occupied_width = max(active_rowspans, default=-1) + 1
        logical_column_count = max(logical_column_count, column, occupied_width)
        rows.append(
            {"row_index": row_index, "source_cell_count": len(cells), "cells": cells}
        )
        active_rowspans = {
            occupied_column: remaining_rows - 1
            for occupied_column, remaining_rows in active_rowspans.items()
            if remaining_rows > 1
        }
        active_rowspans.update(new_rowspans)

    return {
        "row_count": len(rows),
        "logical_column_count": logical_column_count,
        "layout_validation": {
            "no_overlapping_logical_columns": not layout_errors,
            "errors": layout_errors,
        },
        "rows": rows,
    }


def find_by_xpath(root: ElementTree.Element, xpath: str) -> ElementTree.Element:
    """구조 분석 JSON의 단순 XPath를 따라 원본 XML 요소를 다시 찾는다."""
    parts = [part for part in xpath.split("/") if part]
    if not parts or not parts[0].startswith(root.tag + "["):
        raise TableParseError(f"지원하지 않는 XPath입니다: {xpath}")

    current = root
    for part in parts[1:]:
        match = re.fullmatch(r"([A-Za-z0-9-]+)\[(\d+)\]", part)
        if match is None:
            raise TableParseError(f"XPath 형식이 올바르지 않습니다: {xpath}")
        tag, position = match.group(1), int(match.group(2))
        matches = [child for child in current if child.tag == tag]
        if position < 1 or position > len(matches):
            raise TableParseError(f"XPath 위치에 XML 요소가 없습니다: {xpath}")
        current = matches[position - 1]
    return current


def document_map(structure: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """구조 분석 결과의 XML 파일명별 문서 메타데이터를 만든다."""
    return {document["filename"]: document for document in structure.get("documents", [])}


def periods_and_unit(title_table: ElementTree.Element) -> tuple[list[str], str | None]:
    """제목·기간 TABLE에서 원문 기간 문자열과 단위 문자열을 추출한다."""
    text_values = [
        raw_cell_text(cell)
        for row in title_table.iter("TR")
        for cell in row
        if cell.tag in {"TD", "TH", "TU"}
    ]
    periods = [value for value in text_values if PERIOD_PATTERN.search(value)]
    unit = next((value for value in text_values if "단위" in value), None)
    return periods, unit


def period_key(text: str) -> str | None:
    """제57기·제57(당)기처럼 표기된 기간의 공통 기수 키를 반환한다."""
    match = PERIOD_KEY_PATTERN.search(re.sub(r"\s+", "", text))
    return match.group(1) if match else None


def is_amount_cell(text: str) -> bool:
    """원문 문자열을 숫자로 바꾸지 않고 금액 또는 대시 표기인지 판정한다."""
    return bool(AMOUNT_CELL_PATTERN.fullmatch(text.strip()))


def amount_cell_count_by_column(extracted_table: dict[str, Any], columns: list[int]) -> dict[int, int]:
    """지정한 논리 열별 원문 금액·대시 셀 수를 센다."""
    counts = {column: 0 for column in columns}
    for row in extracted_table["rows"]:
        if row["row_index"] == 0:
            continue
        for cell in row["cells"]:
            if not is_amount_cell(cell["text"]):
                continue
            cell_columns = range(
                cell["logical_column_start"],
                cell["logical_column_start"] + cell["colspan"],
            )
            for column in columns:
                if column in cell_columns:
                    counts[column] += 1
    return counts


def build_period_column_mapping(periods: list[str], extracted_table: dict[str, Any]) -> dict[str, Any]:
    """제목 TABLE의 기간을 데이터 TABLE의 실제 금액 열 묶음과 연결한다.

    검증 범위는 기간 표기와 데이터 열의 구조적 대응이다. 금액의 산술적 정확성이나
    회계 기준상 분류의 타당성은 이번 표 추출 단계에서 판정하지 않는다.
    """
    period_by_key = {key: period for period in periods if (key := period_key(period))}
    header_groups = [
        cell
        for row in extracted_table["rows"]
        for cell in row["cells"]
        if cell["tag"] == "TH" and period_key(cell["text"])
    ]

    mappings = []
    mapping_mode = "header_period_groups"
    for group in header_groups:
        key = period_key(group["text"])
        logical_columns = list(
            range(group["logical_column_start"], group["logical_column_start"] + group["colspan"])
        )
        amount_counts = amount_cell_count_by_column(extracted_table, logical_columns)

        mappings.append(
            {
                "period_key": key,
                "title_period": period_by_key.get(key),
                "data_header": group["text"],
                "logical_columns": logical_columns,
                "amount_cell_count_by_column": amount_counts,
                "matched_title_period": key in period_by_key,
                "has_amount_cells": sum(amount_counts.values()) > 0,
            }
        )

    # 자본변동표처럼 기간이 열 머리말이 아니라 날짜가 있는 행으로 표현되는 경우다.
    if not header_groups:
        mapping_mode = "row_date_labels"
        date_rows_by_year: dict[str, list[str]] = {}
        for row in extracted_table["rows"]:
            row_text = " ".join(cell["text"] for cell in row["cells"])
            for year in DATE_ROW_YEAR_PATTERN.findall(row_text):
                date_rows_by_year.setdefault(year, []).append(row_text)
        value_columns = list(range(2, extracted_table["logical_column_count"]))
        amount_counts = amount_cell_count_by_column(extracted_table, value_columns)
        for period in periods:
            year_match = YEAR_PATTERN.search(period)
            year = year_match.group(1) if year_match else None
            matched_rows = date_rows_by_year.get(year or "", [])
            mappings.append(
                {
                    "period_key": period_key(period),
                    "title_period": period,
                    "data_header": None,
                    "logical_columns": value_columns,
                    "amount_cell_count_by_column": amount_counts,
                    "data_row_labels": matched_rows,
                    "matched_title_period": bool(matched_rows),
                    "has_amount_cells": sum(amount_counts.values()) > 0,
                }
            )

    mapped_keys = {mapping["period_key"] for mapping in mappings}
    missing_period_keys = sorted(
        key for key in period_by_key if key not in mapped_keys or not period_by_key[key]
    )
    return {
        "mappings": mappings,
        "validation": {
            "mapping_mode": mapping_mode,
            "periods_from_title_table": len(period_by_key),
            "period_groups_from_data_table": len(mappings),
            "all_title_periods_mapped": not missing_period_keys,
            "missing_period_keys": missing_period_keys,
            "all_mapped_periods_have_amount_cells": bool(mappings) and all(
                mapping["has_amount_cells"] for mapping in mappings
            ),
            "scope": "기간 레이블과 금액 열 묶음의 구조적 대응만 검증",
        },
    }


def validate_preservation(
    source_table: ElementTree.Element, extracted_table: dict[str, Any]
) -> dict[str, Any]:
    """추출 JSON의 셀 텍스트·병합 속성이 원본 TABLE과 같은지 대조한다."""
    source_cells = [
        cell
        for row in source_table.iter("TR")
        for cell in row
        if cell.tag in {"TD", "TH", "TU"}
    ]
    extracted_cells = [cell for row in extracted_table["rows"] for cell in row["cells"]]
    text_matches = len(source_cells) == len(extracted_cells) and all(
        raw_cell_text(source) == extracted["text"]
        for source, extracted in zip(source_cells, extracted_cells)
    )
    spans_match = len(source_cells) == len(extracted_cells) and all(
        source.attrib.get("ROWSPAN", "1") == str(extracted["rowspan"])
        and source.attrib.get("COLSPAN", "1") == str(extracted["colspan"])
        for source, extracted in zip(source_cells, extracted_cells)
    )
    return {
        "source_cell_count": len(source_cells),
        "extracted_cell_count": len(extracted_cells),
        "all_cell_texts_match_source": text_matches,
        "rowspan_colspan_match_source": spans_match,
    }


def section_records_for_extraction(structure: dict[str, Any]) -> list[dict[str, Any]]:
    """연결감사보고서의 연결재무제표 5종만 구조 분석 결과에서 고른다."""
    documents = document_map(structure)
    selected = []
    for section in structure.get("consolidated_sections", []):
        if section.get("section_key") not in CONSOLIDATED_STATEMENTS:
            continue
        source_document = documents.get(section.get("source_xml"), {})
        if source_document.get("role") != "consolidated_audit_report":
            continue
        if not section.get("data_location") or not section.get("title_location"):
            continue
        selected.append(section)

    selected_by_key: dict[str, list[dict[str, Any]]] = {}
    for section in selected:
        selected_by_key.setdefault(section["section_key"], []).append(section)

    duplicates = {
        section_key: records
        for section_key, records in selected_by_key.items()
        if len(records) > 1
    }
    if duplicates:
        details = "; ".join(
            f"{section_key}: "
            + ", ".join(
                f"{record['source_xml']} {record['data_location']['xpath']}"
                for record in records
            )
            for section_key, records in duplicates.items()
        )
        raise TableParseError(f"연결재무제표가 중복 선택되었습니다. 구조 분석 결과를 확인하세요: {details}")

    found_keys = set(selected_by_key)
    missing_keys = set(CONSOLIDATED_STATEMENTS) - found_keys
    if missing_keys:
        missing_text = ", ".join(sorted(missing_keys))
        raise TableParseError(f"구조 분석 JSON에 연결재무제표 위치가 없습니다: {missing_text}")
    return selected


def extract_consolidated_tables(zip_path: Path, structure_path: Path) -> dict[str, Any]:
    """구조 분석 JSON의 XPath를 근거로 연결재무제표 5종을 원본 ZIP에서 추출한다."""
    if not zip_path.is_file():
        raise TableParseError(f"원본 ZIP을 찾지 못했습니다: {zip_path}")
    if not structure_path.is_file():
        raise TableParseError(f"구조 분석 JSON을 찾지 못했습니다: {structure_path}")

    try:
        structure = json.loads(structure_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TableParseError(f"구조 분석 JSON을 읽지 못했습니다: {error}") from error

    selected_sections = section_records_for_extraction(structure)
    documents = document_map(structure)
    extracted_tables = []

    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as error:
        raise TableParseError(f"유효하지 않은 ZIP입니다: {zip_path}") from error

    with archive:
        roots: dict[str, ElementTree.Element] = {}
        for section in selected_sections:
            filename = section["source_xml"]
            if filename not in roots:
                try:
                    raw_xml = archive.read(filename).decode("utf-8")
                except KeyError as error:
                    raise TableParseError(f"ZIP에 구조 분석 대상 XML이 없습니다: {filename}") from error
                roots[filename] = ElementTree.fromstring(normalize_xml_text(raw_xml)[0])

            root = roots[filename]
            title_table = find_by_xpath(root, section["title_location"]["xpath"])
            data_table = find_by_xpath(root, section["data_location"]["xpath"])
            if title_table.tag != "TABLE" or data_table.tag != "TABLE":
                raise TableParseError(f"{section['section_key']}의 위치가 TABLE이 아닙니다.")

            extracted = extract_table_rows(data_table)
            periods, unit = periods_and_unit(title_table)
            raw_cell_preservation = validate_preservation(data_table, extracted)
            period_mapping = build_period_column_mapping(periods, extracted)
            accounting_structure_validation = period_mapping["validation"]
            if not (
                raw_cell_preservation["all_cell_texts_match_source"]
                and raw_cell_preservation["rowspan_colspan_match_source"]
            ):
                raise TableParseError(f"{section['section_key']}의 원문 셀 보존 검증에 실패했습니다.")
            if not extracted["layout_validation"]["no_overlapping_logical_columns"]:
                raise TableParseError(f"{section['section_key']}의 ROWSPAN·COLSPAN 논리 열이 겹칩니다.")
            if not (
                accounting_structure_validation["all_title_periods_mapped"]
                and accounting_structure_validation["all_mapped_periods_have_amount_cells"]
            ):
                raise TableParseError(f"{section['section_key']}의 기간과 금액 열을 대응하지 못했습니다.")

            document = documents[filename]
            extracted_tables.append(
                {
                    "section_key": section["section_key"],
                    "title": section["title"],
                    "consolidation_basis": "consolidated",
                    "source": {
                        "xml_filename": filename,
                        "document_title": document.get("document_title"),
                        "document_role": document.get("role"),
                        "rcept_no": structure["source"]["rcept_no"],
                        "title_location": section["title_location"],
                        "data_location": section["data_location"],
                    },
                    "periods": periods,
                    "unit": unit,
                    "table": extracted,
                    "period_column_mapping": period_mapping["mappings"],
                    # 기존 소비자를 위해 validation 키는 원문 셀 보존 검증으로 유지한다.
                    "validation": raw_cell_preservation,
                    "raw_cell_preservation": raw_cell_preservation,
                    "accounting_structure_validation": accounting_structure_validation,
                }
            )

    return {
        "source": {
            **structure["source"],
            "structure_analysis_path": str(structure_path),
            "extraction_scope": "consolidated_financial_statements_only",
        },
        "tables": extracted_tables,
    }


def default_output_path(result: dict[str, Any]) -> Path:
    """기업·사업연도·접수번호에 따른 처리 결과 JSON 경로를 만든다."""
    source = result["source"]
    return (
        PROJECT_ROOT
        / "data"
        / "processed"
        / "dart"
        / source["company_code"]
        / str(source["fiscal_year"])
        / f"{source['rcept_no']}_consolidated_tables.json"
    )


def save_extraction(result: dict[str, Any], output_path: Path) -> Path:
    """추출 결과를 JSON으로 저장한다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


def main() -> int:
    """명령줄 인자로 구조 JSON과 ZIP을 받아 연결재무제표 JSON을 저장한다."""
    parser = argparse.ArgumentParser(description="OpenDART 연결재무제표 표 추출")
    parser.add_argument("--zip-path", required=True, type=Path, help="OpenDART 원본 ZIP 경로")
    parser.add_argument("--structure-path", required=True, type=Path, help="report_parser 구조 분석 JSON 경로")
    parser.add_argument("--output-path", type=Path, help="결과 JSON 경로")
    args = parser.parse_args()

    try:
        result = extract_consolidated_tables(args.zip_path, args.structure_path)
    except TableParseError as error:
        parser.error(str(error))
    output_path = args.output_path or default_output_path(result)
    save_extraction(result, output_path)
    print(f"추출 결과: {output_path}")
    print(f"연결재무제표 표 수: {len(result['tables'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
