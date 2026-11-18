"""PDF 페이지 텍스트에서 재무제표·주석의 물리 페이지 범위를 자동으로 찾는다.

DART 사업보고서 PDF는 'III. 재무에 관한 사항' 아래에 표준 제목을 쓴다.

    2. 연결재무제표 / 3. 연결재무제표 주석 / 4. 재무제표 / 5. 재무제표 주석 / 6. 배당에 관한 사항

각 제목이 한 줄로 단독 등장하는 페이지를 경계로 삼는다. 목차 줄은 점선(......)과
쪽번호가 붙어 있어 단독 줄 조건에서 걸러진다. 범위 끝에 꼬리말만 있는 페이지는 뺀다.
경계 쪽 안에서 다른 구간 내용을 잘라내는 일은 prepare_pages.py의 자동 자르기가 맡는다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable


class SectionDetectionError(Exception):
    """재무제표·주석 페이지 범위를 자동으로 확정하지 못한 경우."""


HEADING_PATTERNS = {
    "consolidated_statements": r"2\.\s*연결재무제표",
    "consolidated_notes": r"3\.\s*연결재무제표\s*주석",
    "separate_statements": r"4\.\s*재무제표",
    "separate_notes": r"5\.\s*재무제표\s*주석",
    "dividends": r"6\.\s*배당에\s*관한\s*사항",
}

SECTION_NAMES = {
    "consolidated": ("연결재무제표", "연결재무제표 주석"),
    "separate": ("재무제표", "재무제표 주석"),
}

# 범위 안에 실제 재무제표가 있는지 확인할 단어 (해당사항 없음 페이지 걸러내기)
STATEMENT_EVIDENCE = {
    "consolidated": ("연결 재무상태표", "연결재무상태표"),
    "separate": ("재무상태표",),
}

UNIT_PATTERN = re.compile(r"\(\s*단위\s*[:：]\s*(백만원|천원|원)\s*[,)]")
UNIT_SCALE = {"원": 1, "천원": 1_000, "백만원": 1_000_000}

FOOTER_PATTERN = re.compile(r"전자공시시스템|dart\.fss\.or\.kr|^\s*Page\s*\d+\s*\|?\s*$")

# 2-1. 연결 재무상태표 / 4-2. 포괄손익계산서 같은 재무제표 종류별 제목
SUBSECTION_PATTERN = {
    "consolidated": re.compile(r"(?m)^\s*2-\d+\.\s*(연결\s*.+?)\s*$"),
    "separate": re.compile(r"(?m)^\s*4-\d+\.\s*(.+?)\s*$"),
}


@dataclass(frozen=True)
class PageRange:
    start_page: int
    end_page: int
    section: str

    def as_argument(self) -> str:
        """prepare_pages.py --section-range 인자 형식."""
        return f"{self.start_page}-{self.end_page}:{self.section}"


@dataclass
class SectionPlan:
    statement_scope: str
    ranges: list[PageRange]
    unit: str | None
    # BS / IS / CIS / SCE / CF -> 해당 재무제표가 걸친 페이지 목록
    statement_pages: dict[str, list[int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def statements_range(self) -> PageRange:
        return self.ranges[0]

    @property
    def notes_range(self) -> PageRange:
        return self.ranges[1]

    def as_dict(self) -> dict:
        return {
            "statement_scope": self.statement_scope,
            "ranges": [
                {"start_page": r.start_page, "end_page": r.end_page, "section": r.section}
                for r in self.ranges
            ],
            "unit": self.unit,
            "statement_pages": self.statement_pages,
            "notes": self.notes,
        }


def has_body_text(text: str) -> bool:
    """꼬리말(전자공시시스템, Page N)을 빼고 남는 본문이 있는지 확인한다."""

    for line in text.splitlines():
        stripped = line.strip().strip("|").strip()
        if stripped and not FOOTER_PATTERN.search(stripped):
            return True
    return False


def _page_map(pages: Iterable[dict]) -> dict[int, str]:
    page_map: dict[int, str] = {}
    for page in pages:
        number = page.get("page_number")
        text = page.get("text")
        if isinstance(number, int) and isinstance(text, str):
            page_map[number] = text
    if not page_map:
        raise SectionDetectionError("페이지 텍스트가 없습니다.")
    return page_map


def find_heading_pages(page_map: dict[int, str], key: str) -> list[int]:
    """제목이 한 줄에 단독으로 있는 페이지 번호 목록."""

    pattern = re.compile(r"(?m)^\s*" + HEADING_PATTERNS[key] + r"\s*$")
    return sorted(number for number, text in page_map.items() if pattern.search(text))


def _first_after(candidates: list[int], page: int) -> int | None:
    return next((candidate for candidate in candidates if candidate > page), None)


def _trim_trailing_blank(page_map: dict[int, str], start: int, end: int) -> int:
    while end > start and not has_body_text(page_map.get(end, "")):
        end -= 1
    return end


def _has_evidence(page_map: dict[int, str], start: int, end: int, scope: str) -> bool:
    words = STATEMENT_EVIDENCE[scope]
    return any(
        word in page_map.get(number, "")
        for number in range(start, end + 1)
        for word in words
    )


def detect_unit(page_map: dict[int, str], start: int, end: int) -> str | None:
    """재무제표 페이지의 (단위 : 원/천원/백만원) 중 가장 많이 쓰인 값."""

    counter: Counter[str] = Counter()
    for number in range(start, end + 1):
        counter.update(UNIT_PATTERN.findall(page_map.get(number, "")))
    return counter.most_common(1)[0][0] if counter else None


def _statement_kind(title: str) -> str | None:
    compact = re.sub(r"\s+", "", title)
    if "재무상태표" in compact:
        return "BS"
    if "포괄손익계산서" in compact:
        return "CIS"
    if "손익계산서" in compact:
        return "IS"
    if "자본변동표" in compact:
        return "SCE"
    if "현금흐름표" in compact:
        return "CF"
    return None


def detect_statement_pages(
    page_map: dict[int, str], statements: PageRange, scope: str, last_page: int | None = None
) -> dict[str, list[int]]:
    """재무상태표·손익계산서 등 종류별로 걸친 페이지를 찾는다.

    제목 페이지부터 다음 제목 페이지까지(경계 쪽 포함)를 그 재무제표의 페이지로 본다.
    마지막 재무제표(보통 현금흐름표)는 주석 시작 쪽(last_page)까지 본다. 주석 첫 쪽
    위쪽에 현금흐름표 끝부분이 남아 있는 경우가 있기 때문이다.
    """

    starts: list[tuple[int, str]] = []
    for number in range(statements.start_page, statements.end_page + 1):
        for match in SUBSECTION_PATTERN[scope].finditer(page_map.get(number, "")):
            kind = _statement_kind(match.group(1))
            if kind:
                starts.append((number, kind))

    result: dict[str, list[int]] = {}
    for index, (start, kind) in enumerate(starts):
        end = (
            starts[index + 1][0]
            if index + 1 < len(starts)
            else (last_page or statements.end_page)
        )
        pages = result.setdefault(kind, [])
        for number in range(start, max(start, end) + 1):
            if number not in pages:
                pages.append(number)
    return result


def _plan(page_map: dict[int, str], scope: str) -> SectionPlan | None:
    if scope == "consolidated":
        statement_heads = find_heading_pages(page_map, "consolidated_statements")
        notes_heads = find_heading_pages(page_map, "consolidated_notes")
        end_heads = find_heading_pages(page_map, "separate_statements")
    else:
        statement_heads = find_heading_pages(page_map, "separate_statements")
        notes_heads = find_heading_pages(page_map, "separate_notes")
        end_heads = find_heading_pages(page_map, "dividends")

    for notes_start in notes_heads:
        before = [page for page in statement_heads if page < notes_start]
        if not before:
            continue
        statements_start = before[-1]
        end_heading = _first_after(end_heads, notes_start)
        if end_heading is None:
            continue

        statements_end = _trim_trailing_blank(page_map, statements_start, notes_start - 1)
        notes_end = _trim_trailing_blank(page_map, notes_start, end_heading - 1)
        if notes_end <= notes_start and not has_body_text(page_map.get(notes_start, "")):
            continue
        if not _has_evidence(page_map, statements_start, statements_end, scope):
            continue

        names = SECTION_NAMES[scope]
        statements = PageRange(statements_start, statements_end, names[0])
        notes = PageRange(notes_start, notes_end, names[1])
        plan = SectionPlan(
            statement_scope=scope,
            ranges=[statements, notes],
            unit=detect_unit(page_map, statements_start, statements_end),
        )
        plan.statement_pages = detect_statement_pages(
            page_map, statements, scope, last_page=notes_start
        )
        if notes_end != end_heading - 1:
            plan.notes.append(
                f"{notes_end + 1}~{end_heading - 1}쪽은 꼬리말만 있어 제외"
            )
        return plan
    return None


def detect_sections(pages: Iterable[dict], scope: str = "auto") -> SectionPlan:
    """pdf_pages.json의 pages 배열로 재무제표·주석 범위를 정한다.

    scope="auto"면 연결재무제표가 있으면 연결, 없으면 별도를 쓴다.
    """

    if scope not in {"auto", "consolidated", "separate"}:
        raise ValueError("scope는 auto, consolidated, separate 중 하나여야 합니다.")

    page_map = _page_map(pages)
    order = ["consolidated", "separate"] if scope == "auto" else [scope]

    for candidate in order:
        plan = _plan(page_map, candidate)
        if plan is not None:
            if scope == "auto" and candidate == "separate":
                plan.notes.append("연결재무제표를 찾지 못해 별도 기준으로 진행")
            return plan

    found = {
        key: find_heading_pages(page_map, key)[:5] for key in HEADING_PATTERNS
    }
    raise SectionDetectionError(
        "재무제표·주석 범위를 자동으로 찾지 못했습니다. 제목이 발견된 페이지: "
        + ", ".join(f"{key}={pages}" for key, pages in found.items())
    )
