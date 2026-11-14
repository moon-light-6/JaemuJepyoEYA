"""
[OpenDART 사업보고서 ZIP의 XML 문서 구조와 연결재무제표 위치 분석]

원본 OpenDART XML엔 R&D처럼 이스케이프되지 않은 특수문자나 본문 안의 < 표 제목 > 이 섞일 수 있음
이 모듈은 원본 파일을 고치지 않고 메모리에서만 XML 1.0 형식에 맞게 최소한으로 정규화한 뒤 구조를 읽음
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[3]

DOCUMENT_ROLES = {
    "사업보고서": "business_report",
    "감사보고서": "separate_audit_report",
    "연결감사보고서": "consolidated_audit_report",
}

CONSOLIDATED_STATEMENTS = {
    "consolidated_statement_of_financial_position": "연결재무상태표",
    "consolidated_income_statement": "연결손익계산서",
    "consolidated_statement_of_comprehensive_income": "연결포괄손익계산서",
    "consolidated_statement_of_changes_in_equity": "연결자본변동표",
    "consolidated_statement_of_cash_flows": "연결현금흐름표",
}
CONSOLIDATED_NOTES = "consolidated_financial_statement_notes"

# XML 예약문자, 제어문자만 고침 | 태그와 표 셀의 원문 내용은 그대로 보존
UNESCAPED_AMPERSAND = re.compile(r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9._-]*;)")
BARE_LESS_THAN = re.compile(r"<(?!(?:/?[A-Za-z_][A-Za-z0-9_.:-]*\b|!--|!\[CDATA\[|\?))")
INVALID_XML_CONTROL = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


class ReportParseError(Exception):
    """ZIP 또는 OpenDART XML 구조를 읽을 수 없을 때 발생한다."""

# XML 텍스트를 최소한으로 정규화 | & < 제어문자만 수정
def normalize_xml_text(xml_text: str) -> tuple[str, dict[str, int]]:
    """
    파싱을 방해하는 원문 표기만 메모리에서 정규화

    OpenDART가 제공한 원본 XML 파일은 수정 or ZIP에 다시 저장 X
    반환한 통계는 결과 JSON에 기록해 어떤 복구가 있었는지 추적
    """
    xml_text, ampersand_count = UNESCAPED_AMPERSAND.subn("&amp;", xml_text)
    xml_text, less_than_count = BARE_LESS_THAN.subn("&lt;", xml_text)
    xml_text, control_count = INVALID_XML_CONTROL.subn("", xml_text)
    return xml_text, {
        "escaped_ampersands": ampersand_count,
        "escaped_bare_less_than": less_than_count,
        "removed_invalid_control_characters": control_count,
    }

# XML 요소의 텍스트를 공백 하나로 연결 | 표·제목용
def compact_text(element: ElementTree.Element) -> str:
    """표·제목의 분리된 텍스트 노드를 공백 하나로 연결한다."""
    return " ".join(" ".join(element.itertext()).split())

# 제목 비교용 (공백, 줄바꿈 제거)
def normalized_label(text: str) -> str:
    """제목 비교를 위해 공백과 줄바꿈을 제거한다."""
    return re.sub(r"\s+", "", text)

# 문서 순서, XPath 형태 위치 부여 (TOC ID 포함)
def build_locations(root: ElementTree.Element) -> tuple[list[ElementTree.Element], dict[int, dict[str, Any]]]:
    """문서 순서와 XPath 형태의 위치를 각 XML 요소에 부여한다."""
    ordered: list[ElementTree.Element] = []
    locations: dict[int, dict[str, Any]] = {}

    def visit(element: ElementTree.Element, xpath: str) -> None:
        index = len(ordered)
        ordered.append(element)
        locations[id(element)] = {
            "element_index": index,
            "xpath": xpath,
            "toc_id": element.attrib.get("ATOCID") or None,
        }
        same_tag_count: dict[str, int] = {}
        for child in element:
            same_tag_count[child.tag] = same_tag_count.get(child.tag, 0) + 1
            visit(child, f"{xpath}/{child.tag}[{same_tag_count[child.tag]}]")

    visit(root, f"/{root.tag}[1]")
    return ordered, locations

# 문서 제목으로 문서 역할 분류 -> DOCUMENT-NAME 기준
def classify_document(document_title: str) -> str:
    """DOCUMENT-NAME으로 문서의 역할을 구분한다."""
    return DOCUMENT_ROLES.get(normalized_label(document_title), "other")

# 목차 구조 추출 -> ATOC=Y인 구조 제목 기준
def extract_toc_sections(
    ordered: list[ElementTree.Element], locations: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """ATOC=Y인 구조 제목을 추출해 문서의 목차 구조를 기록한다."""
    sections = []
    for element in ordered:
        if element.attrib.get("ATOC") != "Y":
            continue
        title = compact_text(element)
        if title:
            sections.append({"title": title, "tag": element.tag, "location": locations[id(element)]})
    return sections

# 실제 재무제표 표 판정 (목차 언급 아닌 본문 표 검증)
def is_substantive_statement_table(
    table: ElementTree.Element,
    title: str,
    fiscal_year: int,
) -> bool:
    """
    연결재무제표의 실제 데이터 표인지 판정

    검사 조건:
    1. 표 앞부분에 재무제표 제목 존재
    2. 대상 사업연도 표시 존재
    3. 표 행이 3개 이상
    4. 계정과목과 숫자 금액이 있는 행이 3개 이상
    """
    table_text = normalized_label(compact_text(table))

    title_position = table_text.find(title)
    has_title = 0 <= title_position <= 250
    has_year = f"{fiscal_year}년" in table_text

    row_count = sum(
        1 for element in table.iter()
        if element.tag == "TR"
    )

    return (
        has_title
        and has_year
        and row_count >= 3
    )


# 검증된 연결재무제표 표의 위치와 본문 근거 기록
def find_statement_data_table(
    header_table: ElementTree.Element,
    parent_map: dict[int, ElementTree.Element],
) -> ElementTree.Element | None:
    """제목 TABLE과 같은 부모의 다음 데이터 TABLE을 확인한다.

    제목·기간 TABLE 뒤의 첫 TABLE만 조사해, 다른 섹션의 표를 임의로 연결하지
    않는다. 데이터 표에는 계정과목·금액 행이 세 행 이상 있어야 한다.
    """
    parent = parent_map.get(id(header_table))
    if parent is None:
        return None

    siblings = list(parent)
    header_position = siblings.index(header_table)
    for sibling in siblings[header_position + 1 :]:
        if sibling.attrib.get("ATOC") == "Y":
            return None
        if sibling.tag == "TABLE":
            return sibling if count_financial_rows(sibling) >= 3 else None
    return None


def statement_record(
    section_key: str,
    title: str,
    filename: str,
    header_table: ElementTree.Element,
    data_table: ElementTree.Element,
    locations: dict[int, dict[str, Any]],
    fiscal_year: int,
) -> dict[str, Any]:
    """검증된 연결재무제표 표 한 개의 위치와 본문 근거를 만든다."""
    return {
        "section_key": section_key,
        "title": title,
        "source_xml": filename,
        "present": True,
        "location": locations[id(header_table)],
        "title_location": locations[id(header_table)],
        "data_location": locations[id(data_table)],
        "evidence": {
            "method": "본문 표 제목·사업연도·행 수 검증",
            "title_position_in_table": normalized_label(compact_text(header_table)).find(title),
            "fiscal_year_found": f"{fiscal_year}년" in normalized_label(compact_text(header_table)),
            "title_table_row_count": sum(1 for element in header_table.iter() if element.tag == "TR"),
            "data_table_row_count": sum(1 for element in data_table.iter() if element.tag == "TR"),
            "financial_row_count": count_financial_rows(data_table),
        },
    }


# 모든 TABLE에서 실제 연결재무제표 다섯 표 위치 기록
def find_statement_sections(
    filename: str,
    ordered: list[ElementTree.Element],
    locations: dict[int, dict[str, Any]],
    parent_map: dict[int, ElementTree.Element],
    fiscal_year: int,
) -> list[dict[str, Any]]:
    """모든 TABLE에서 실제 연결재무제표 다섯 표를 찾아 위치를 기록한다."""
    sections = []
    for header_table in ordered:
        if header_table.tag != "TABLE":
            continue
        for section_key, title in CONSOLIDATED_STATEMENTS.items():
            if not is_substantive_statement_table(header_table, title, fiscal_year):
                continue
            data_table = find_statement_data_table(header_table, parent_map)
            if data_table is None:
                continue
            sections.append(
                statement_record(
                    section_key,
                    title,
                    filename,
                    header_table,
                    data_table,
                    locations,
                    fiscal_year,
                )
            )
    return sections

# 주석 제목 뒤의 본문 근거 조사
def note_body_evidence(
    heading: ElementTree.Element,
    ordered: list[ElementTree.Element],
    locations: dict[int, dict[str, Any]],
) -> dict[str, int | bool | str]:
    """
    주석 제목 뒤의 본문 검사!

    동일하거나 상위 수준의 다음 목차 제목을 임시 종료 경계로 사용

    XML 구조에 따라 실제 섹션 종료 위치와 다를 수 있으니까, 이 결과만으로 주석 전체가 완전하다고 판정 X
    """
    heading_location = locations[id(heading)]
    start = heading_location["element_index"] + 1
    heading_depth = heading_location["xpath"].count("/")

    end = len(ordered)
    boundary_found = False

    for element in ordered[start:]:
        if element.attrib.get("ATOC") != "Y":
            continue

        element_depth = locations[id(element)]["xpath"].count("/")

        # 같은 수준 또는 상위 목차가 나오면 검사 종료
        if element_depth <= heading_depth:
            end = locations[id(element)]["element_index"]
            boundary_found = True
            break

    body_elements = ordered[start:end]

    table_count = sum(
        1 for element in body_elements
        if element.tag == "TABLE"
    )

    paragraph_count = sum(
        1 for element in body_elements
        if element.tag == "P" and compact_text(element)
    )

    return {
        "start_element_index": start,
        "end_element_index_exclusive": end,
        "end_boundary_found": boundary_found,
        "nearby_table_count": table_count,
        "nearby_paragraph_count": paragraph_count,
        "has_substantive_body": (
            table_count > 0 and paragraph_count > 0
        ),
        "boundary_method": "same_or_higher_level_toc",
    }


# 연결재무제표 주석 구조와 본문 검증
def find_consolidated_notes(
    filename: str,
    role: str,
    ordered: list[ElementTree.Element],
    locations: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """연결재무제표 주석의 구조 제목과 이어지는 본문 표·문단을 함께 검증한다."""
    records = []
    for heading in ordered:
        if heading.attrib.get("ATOC") != "Y":
            continue
        title = compact_text(heading)
        label = normalized_label(title)
        is_main_report_notes = "연결재무제표주석" in label
        is_consolidated_audit_notes = role == "consolidated_audit_report" and label == "주석"
        if not (is_main_report_notes or is_consolidated_audit_notes):
            continue
        evidence = note_body_evidence(heading, ordered, locations)
        if evidence["has_substantive_body"]:
            records.append(
                {
                    "section_key": CONSOLIDATED_NOTES,
                    "title": title,
                    "source_xml": filename,
                    "present": True,
                    "location": locations[id(heading)],
                    "evidence": {"method": "구조 제목 뒤의 표·문단 검증", **evidence},
                }
            )
    return records

# 숫자 형식의 금액 셀 검사
# 예: 1234, 1,234, -1234, (1,234), 123.45
AMOUNT_PATTERN = re.compile(
    r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"|\((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)"
)

# 재무제표 표 행 개수 확인
def count_financial_rows(table: ElementTree.Element) -> int:
    """계정과목과 숫자 금액이 함께 있는 표 행의 개수를 확인한다."""
    count = 0

    for row in table.iter("TR"):
        # 표 행의 직접적인 셀만 확인
        cells = [
            compact_text(cell).strip()
            for cell in row
            if cell.tag in {"TD", "TH"}
        ]

        if len(cells) < 2:
            continue

        # 첫 번째 셀에 계정명 같은 텍스트가 있는지 확인
        account_name = cells[0]
        has_account = bool(account_name) and not AMOUNT_PATTERN.fullmatch(
            account_name.replace(" ", "")
        )

        # 나머지 셀 중 숫자 형식의 금액이 있는지 확인
        has_amount = any(
            AMOUNT_PATTERN.fullmatch(cell.replace(" ", ""))
            for cell in cells[1:]
        )

        if has_account and has_amount:
            count += 1
    return count

# 사업보고서 ZIP 분석
def analyze_report_zip(
    zip_path: Path, company_code: str, fiscal_year: int, rcept_no: str
) -> dict[str, Any]:
    """사업보고서 ZIP 안의 모든 XML을 분석해 구조와 연결재무제표 검증 결과를 반환한다."""
    if not zip_path.is_file():
        raise ReportParseError(f"원본 ZIP을 찾지 못했습니다: {zip_path}")

    documents = []
    found_sections: list[dict[str, Any]] = []
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as error:
        raise ReportParseError(f"유효하지 않은 ZIP입니다: {zip_path}") from error

    with archive:
        xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
        if not xml_names:
            raise ReportParseError("ZIP 안에 XML 문서가 없습니다.")
        for filename in xml_names:
            raw_xml = archive.read(filename).decode("utf-8")
            prepared_xml, repairs = normalize_xml_text(raw_xml)
            try:
                root = ElementTree.fromstring(prepared_xml)
            except ElementTree.ParseError as error:
                raise ReportParseError(f"{filename} XML을 읽지 못했습니다: {error}") from error

            ordered, locations = build_locations(root)
            parent_map = {
                id(child): parent
                for parent in root.iter()
                for child in parent
            }
            # DOCUMENT-NAME 누락 시 명확한 오류 발생
            title_element = root.find("DOCUMENT-NAME")

            if title_element is None:
                raise ReportParseError(f"{filename}: DOCUMENT-NAME 요소가 없습니다.")

            document_title = compact_text(title_element)
            role = classify_document(document_title)
            documents.append(
                {
                    "filename": filename,
                    "document_title": document_title,
                    "role": role,
                    "element_count": len(ordered),
                    "parse_repairs": repairs,
                    "toc_sections": extract_toc_sections(ordered, locations),
                }
            )
            found_sections.extend(
                find_statement_sections(filename, ordered, locations, parent_map, fiscal_year)
            )
            found_sections.extend(find_consolidated_notes(filename, role, ordered, locations))

    # 연결재무제표 5종의 식별 키
    required_statements = set(CONSOLIDATED_STATEMENTS)

    # XML 파일별로 발견한 섹션을 분리
    sections_by_document = {}

    for document in documents:
        filename = document["filename"]

        # 별도 감사보고서는 연결재무제표 검증 대상에서 제외
        if document["role"] not in {
            "business_report",
            "consolidated_audit_report",
        }:
            continue

        sections_by_document[filename] = {
            section["section_key"]
            for section in found_sections
            if section["source_xml"] == filename
        }

    # 동일한 XML 안에 연결재무제표 5종이 있는지 확인
    complete_statement_documents = [
        filename
        for filename, keys in sections_by_document.items()
        if required_statements.issubset(keys)
    ]

    # 재무제표 5종이 존재하는 동일 문서의 주석 확인
    note_source_documents = [
        filename
        for filename, keys in sections_by_document.items()
        if CONSOLIDATED_NOTES in keys
    ]
    notes_present = bool(note_source_documents)

    return {
        "source": {
            "zip_path": str(zip_path),
            "company_code": company_code,
            "fiscal_year": fiscal_year,
            "rcept_no": rcept_no,
        },
        "documents": documents,
        "consolidated_sections": found_sections,
        "verification": {
            "all_consolidated_financial_statements_present": bool(
                complete_statement_documents
            ),
            "consolidated_financial_statement_notes_present": notes_present,
            "complete_statement_source_xml": complete_statement_documents,
            "consolidated_notes_source_xml": note_source_documents,
            "verification_basis": (
                "동일 XML에서 연결재무제표 5종을 확인하고, "
                "해당 XML의 주석 본문 존재 여부를 검사"
            ),
            "notes_completeness_verified": False,
        },
    }

# 분석 결과 JSON 저장
def save_analysis(result: dict[str, Any], output_path: Path) -> Path:
    """분석 결과를 data/processed 아래 JSON 파일로 저장한다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path

# 기본 출력 경로 생성
def default_output_path(company_code: str, fiscal_year: int, rcept_no: str) -> Path:
    """기업·연도·접수번호에 따라 재사용 가능한 처리 결과 경로를 만든다."""
    return PROJECT_ROOT / "data" / "processed" / "dart" / company_code / str(fiscal_year) / f"{rcept_no}_structure.json"

# 명령줄 인터페이스
def main() -> int:
    """명령줄에서 원본 ZIP을 분석하고 JSON 저장 위치를 출력한다."""
    parser = argparse.ArgumentParser(description="OpenDART XML 구조와 연결재무제표 위치 분석")
    parser.add_argument("--zip-path", required=True, type=Path, help="OpenDART 원본 ZIP 경로")
    parser.add_argument("--corp-code", required=True, help="OpenDART 고유번호")
    parser.add_argument("--fiscal-year", required=True, type=int, help="사업연도")
    parser.add_argument("--rcept-no", required=True, help="접수번호")
    parser.add_argument("--output-path", type=Path, help="결과 JSON 경로")
    args = parser.parse_args()

    try:
        result = analyze_report_zip(args.zip_path, args.corp_code, args.fiscal_year, args.rcept_no)
    except ReportParseError as error:
        parser.error(str(error))
    output_path = args.output_path or default_output_path(args.corp_code, args.fiscal_year, args.rcept_no)
    save_analysis(result, output_path)
    verification = result["verification"]
    print(f"분석 결과: {output_path}")
    print(f"연결재무제표 5종: {verification['all_consolidated_financial_statements_present']}")
    print(f"연결재무제표 주석: {verification['consolidated_financial_statement_notes_present']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
