
# 필요한 페이지만 추출 - 예) 삼성전자 사업보고서에서 연결재무제표 + 연결 재무제표 분석까지만 추출할 때 이용

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECTION_RANGE_PATTERN = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*:\s*(.+?)\s*$")


@dataclass(frozen=True)
class SectionRange: # 페이지 범위 
    start_page: int
    end_page: int
    section: str

# 사람이 쓴 범위 읽기
def parse_section_range(value: str) -> SectionRange: 
    
    match = SECTION_RANGE_PATTERN.match(value)
    if not match:
        raise argparse.ArgumentTypeError(
            "형식은 '시작페이지-끝페이지:섹션명'이어야 합니다. 예: 83-90:연결재무제표"
        )

    start_page, end_page = int(match.group(1)), int(match.group(2))
    if start_page > end_page:
        raise argparse.ArgumentTypeError("시작 페이지는 끝 페이지보다 작거나 같아야 합니다.")
    return SectionRange(start_page, end_page, match.group(3))

# 페이지 범위 겹치지 않는지 체크 
def validate_ranges(ranges: list[SectionRange]) -> None:
  
    ordered = sorted(ranges, key=lambda page_range: page_range.start_page)
    for previous, current in zip(ordered, ordered[1:]):
        if current.start_page <= previous.end_page:
            raise ValueError(
                "페이지 범위가 겹칩니다: "
                f"{previous.start_page}-{previous.end_page}, "
                f"{current.start_page}-{current.end_page}"
            )


# 특정 페이지가 어느 섹션인지 찾기  
def section_for_page(page_number: int, ranges: list[SectionRange]) -> str | None:
    for page_range in ranges:
        if page_range.start_page <= page_number <= page_range.end_page:
            return page_range.section
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PDF 페이지 텍스트를 검색용 페이지 JSON으로 표준화합니다."
    )
    parser.add_argument("--company", required=True, help="기업명")
    parser.add_argument("--corp-code", required=True, help="OpenDART 기업 고유번호")
    parser.add_argument("--fiscal-year", required=True, type=int, help="사업연도")
    parser.add_argument("--rcept-no", required=True, help="DART 접수번호")
    parser.add_argument(
        "--statement-scope",
        required=True,
        choices=["consolidated", "separate"],
        help="연결 또는 별도 기준",
    )
    parser.add_argument(
        "--document-type",
        default="financial_statement",
        help="문서 유형 (기본값: financial_statement)",
    )
    parser.add_argument(
        "--section-range",
        required=True,
        action="append",
        type=parse_section_range,
        help="선택할 페이지 범위. 여러 번 지정 가능. 예: 83-90:연결재무제표",
    )
    parser.add_argument("--input-path", type=Path, help="PDF 페이지 추출 JSON 경로")
    parser.add_argument("--output-path", type=Path, help="출력 JSON 경로")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    validate_ranges(args.section_range)

    # 입력 파일 경로를 정하기 
    input_path = args.input_path or (
        PROJECT_ROOT
        / "data"
        / "processed"
        / "dart"
        / args.corp_code
        / str(args.fiscal_year)
        / f"{args.rcept_no}_pdf_pages.json"
    )
    output_path = args.output_path or input_path.with_name(f"{args.rcept_no}_pages.json") # 출력 파일 경로 설정 

    with input_path.open(encoding="utf-8") as file: 
        pdf_data = json.load(file)
    pages = pdf_data.get("pages")
    if not isinstance(pages, list):
        raise ValueError("입력 JSON에 pages 배열이 없습니다.")

    records: list[dict[str, object]] = []
    for page in pages:
        page_number = page.get("page_number")
        text = page.get("text")
        if not isinstance(page_number, int):
            raise ValueError("페이지 번호가 올바르지 않습니다.")

        section = section_for_page(page_number, args.section_range)
        if section is None:
            continue
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{page_number}쪽의 텍스트가 비어 있습니다.")

        records.append(
            {
                "page_id": (
                    f"{args.corp_code}_{args.fiscal_year}_"
                    f"{args.statement_scope}_{page_number:04d}"
                ),
                "text": text,
                "company": args.company,
                "fiscal_year": args.fiscal_year,
                "statement_scope": args.statement_scope,
                "document_type": args.document_type,
                "source_file": f"{args.rcept_no}.pdf",
                "pdf_page": page_number,
                "section": section,
            }
        )

    if not records:
        raise ValueError("선택된 페이지가 없습니다. 페이지 범위와 입력 파일을 확인하세요.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(records, file, ensure_ascii=False, indent=2)

    print(f"입력 PDF 페이지 수: {len(pages)}")
    print(f"선택된 페이지 수: {len(records)}")
    print(f"출력 파일: {output_path}")


if __name__ == "__main__":
    main()
  
