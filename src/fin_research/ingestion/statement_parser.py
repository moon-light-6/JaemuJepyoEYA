"""PDF 쪽 텍스트(`*_pages.json`)에서 연결재무제표 4종의 수치를 뽑는 파서.

DB 없이 동작한다. 결과(ParseResult)를 scripts/storage/load_financials.py 가 PostgreSQL에 넣는다.

텍스트 규칙(에코프로비엠 2025 사업보고서에서 확인):
- 표 제목 줄(예: '연결 재무상태표') 바로 다음 줄이 '제 10 기 2025.12.31 현재' 형태의 기간 줄이다.
  (2-1. 같은 번호 붙은 제목 줄은 쪽 상단에 다음 표 제목이 미리 나오는 경우가 있어 무시한다.)
- 행 = 계정명 줄 + 값 N줄. N은 기간 수(보통 3), 자본변동표는 구성요소 열 수(8).
- 빈 값은 전각 공백(U+3000) 한 줄, 음수는 괄호, '-'는 값 없음 표기.
- 계정명이 두 줄로 쪼개지면(다음 줄이 값이 아니면) 이어 붙인다.
- 들여쓰기는 계정명 앞의 전각 공백 개수.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

FW = "　"
STATEMENT_SECTION = "연결재무제표"
ROLES = ("current", "prior", "prior2")
UNIT_MULTIPLIERS = {"원": 1, "천원": 1000, "백만원": 1_000_000}

TITLE_TYPES = (
    (re.compile(r"^연결재무상태표$"), "BS"),
    (re.compile(r"^연결손익계산서$"), "IS"),
    (re.compile(r"^연결포괄손익계산서$"), "CIS"),
    (re.compile(r"^연결자본변동표$"), "SCE"),
    (re.compile(r"^연결현금흐름표$"), "CF"),
)
NOISE_RES = (
    re.compile(r"^전자공시시스템"),
    re.compile(r"^Page\s*\d+$"),
    re.compile(r"^\d-\d+\.\s*\S"),  # '2-2. 연결 포괄손익계산서' 같은 목차식 제목
    re.compile(r"^\d+\.\s*연결재무제표$"),
    re.compile(r"^\d+\.\s*연결재무제표\s*주석"),  # 주석 첫 쪽 상단 제목
    re.compile(r"^\d+\.\s*일반사항"),
)
PERIOD_RE = re.compile(
    r"제\s*(\d+)\s*기\s+(?:(\d{4}\.\d{2}\.\d{2})\s*부터\s*)?(\d{4}\.\d{2}\.\d{2})"
)
COLUMN_HEADER_RE = re.compile(r"^제\s*\d+\s*기$")
UNIT_RE = re.compile(r"\(단위\s*[:：]\s*(원|천원|백만원)\)")
NUM_RE = re.compile(r"^\(?-?\d[\d,]*(?:\.\d+)?\)?$")
DASHES = {"-", "－", "―"}
NOTE_REF_RE = re.compile(r"\(주\s*([\d,\s\-]+)\)\s*$")
SCE_OPEN_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\s*\(기초")


@dataclass
class ParsedValue:
    period_idx: int
    column_key: str
    raw: str | None
    status: str  # ok / empty / dash
    reported: int | None
    amount: int | None
    pdf_page: int
    chunk_id: str | None


@dataclass
class ParsedLine:
    line_no: int
    label_raw: str
    label_norm: str
    indent: int
    is_total: bool
    note_refs: str | None
    pdf_page: int
    chunk_id: str | None
    values: list[ParsedValue] = field(default_factory=list)


@dataclass
class ParsedStatement:
    statement_type: str
    title_raw: str
    unit_label_raw: str
    unit_multiplier: int
    periods: list[dict[str, Any]]  # term_no, start, end (date)
    lines: list[ParsedLine]
    pdf_page_start: int
    pdf_page_end: int


@dataclass
class ParseResult:
    statements: list[ParsedStatement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def to_date(text: str | None) -> date | None:
    if not text:
        return None
    y, m, d = text.split(".")
    return date(int(y), int(m), int(d))


def is_blank_value(raw: str) -> bool:
    return raw.strip() == ""


def is_value_token(raw: str) -> bool:
    s = raw.strip()
    return s == "" or s in DASHES or bool(NUM_RE.match(s))


def parse_amount(raw: str) -> tuple[str, int | None]:
    """값 토큰 -> (상태, 표시된 정수). 상태: ok / empty / dash / unsupported(소수 등)."""
    s = raw.strip()
    if s == "":
        return "empty", None
    if s in DASHES:
        return "dash", None
    if not NUM_RE.match(s):
        return "unsupported", None
    negative = s.startswith("(") or s.startswith("-")
    body = s.strip("()-").replace(",", "")
    if "." in body:
        number = Decimal(body)
        if number != number.to_integral_value():
            return "unsupported", None  # 주당순이익의 소수 등. 현재 스키마는 정수만 저장
        body = str(int(number))
    value = int(body)
    return "ok", -value if negative else value


def statement_type_of(raw: str) -> str | None:
    compact = re.sub(r"\s+", "", raw)
    for pattern, statement_type in TITLE_TYPES:
        if pattern.match(compact):
            return statement_type
    return None


def collect_lines(pages: list[dict[str, Any]], extra_pages: tuple[int, ...] = ()) -> list[tuple[int, str]]:
    """연결재무제표 쪽(과 extra_pages)의 텍스트를 (pdf_page, 줄) 목록으로 만든다. 잡음 줄은 버린다."""
    lines: list[tuple[int, str]] = []
    for page in sorted(pages, key=lambda p: p["pdf_page"]):
        if page.get("section") != STATEMENT_SECTION and page["pdf_page"] not in extra_pages:
            continue
        for raw in page["text"].split("\n"):
            raw = raw.rstrip(" \t\r")  # 전각 공백(빈 값 칸)은 남겨야 하므로 일반 공백만 제거
            stripped = raw.strip()
            if stripped == "":
                if FW in raw:
                    lines.append((page["pdf_page"], FW))  # 빈 값 칸
                continue  # 일반 공백만 있는 줄은 잡음
            if any(pattern.match(stripped) for pattern in NOISE_RES):
                continue
            lines.append((page["pdf_page"], raw))
    return lines


def find_chunk_id(
    chunks_by_page: dict[int, list[dict[str, Any]]],
    pdf_page: int,
    label_lines: list[str],
    tokens: list[str],
) -> str | None:
    chunks = chunks_by_page.get(pdf_page, [])
    first = label_lines[0]
    needles = [
        "\n".join(label_lines + tokens),
        "\n".join(label_lines + tokens[:1]),
        first + ("\n" + tokens[0] if tokens else ""),
        first,
        first.strip(),
    ]
    for needle in needles:
        if not needle.strip():
            continue
        for chunk in chunks:
            if needle in chunk["text"]:
                return chunk["chunk_id"]
    return None


def split_label(label_lines: list[str]) -> tuple[str, int, str | None, str | None]:
    """계정명 줄들 -> (라벨, 들여쓰기, 주석참조, 줄 안의 단위)."""
    first = label_lines[0]
    indent = len(first) - len(first.lstrip(FW))
    text = "".join(line.strip() for line in label_lines)
    inline_unit = None
    unit_match = UNIT_RE.search(text)
    if unit_match:
        inline_unit = unit_match.group(1)
        text = UNIT_RE.sub("", text).strip()
    note_refs = None
    note_match = NOTE_REF_RE.search(text)
    if note_match:
        note_refs = re.sub(r"\s+", "", note_match.group(1))
        text = text[: note_match.start()].rstrip()
    return text, indent, note_refs, inline_unit


def parse_statement(
    statement_type: str,
    lines: list[tuple[int, str]],
    start: int,
    end: int,
    chunks_by_page: dict[int, list[dict[str, Any]]],
    warnings: list[str],
) -> ParsedStatement | None:
    title_page, title_raw = lines[start]
    j = start + 1
    periods: list[dict[str, Any]] = []
    while j < end and (match := PERIOD_RE.search(lines[j][1].strip())):
        periods.append(
            {"term_no": int(match.group(1)), "start": to_date(match.group(2)), "end": to_date(match.group(3))}
        )
        j += 1

    unit_label = None
    unit_name = None
    sce_headers: list[str] = []
    while j < end:
        raw = lines[j][1]
        stripped = raw.strip()
        unit_match = UNIT_RE.search(stripped)
        if unit_match and unit_name is None:
            unit_name = unit_match.group(1)
            unit_label = unit_match.group(0)
        elif is_blank_value(raw) or COLUMN_HEADER_RE.match(stripped):
            pass
        elif statement_type == "SCE" and not SCE_OPEN_RE.match(stripped):
            sce_headers.append(stripped)
        else:
            break
        j += 1

    if unit_name is None:
        warnings.append(f"[{statement_type}] 단위(원/천원/백만원)를 찾지 못해 건너뜀 (pdf_page {title_page})")
        return None
    multiplier = UNIT_MULTIPLIERS[unit_name]

    # 값 열 정의
    if statement_type == "SCE":
        if len(sce_headers) != 10:
            warnings.append(
                f"[SCE] 열 머리글이 예상(10개)과 달라 건너뜀: {len(sce_headers)}개 {sce_headers}"
            )
            return None
        # 머리글 순서: 자본/지배기업 소유주 귀속분/비지배지분/자본 합계/자본금..지배기업합계
        # 값 순서: 자본금..지배기업합계/비지배지분/자본 합계
        columns = sce_headers[4:] + sce_headers[2:4]
    else:
        columns = [""] * len(periods)
    n_values = len(columns)
    if n_values == 0:
        warnings.append(f"[{statement_type}] 기간 줄을 찾지 못해 건너뜀 (pdf_page {title_page})")
        return None

    parsed_lines: list[ParsedLine] = []
    block_period_idx = 0
    pos = j
    while pos < end:
        raw = lines[pos][1]
        if is_value_token(raw):
            if not is_blank_value(raw):
                warnings.append(f"[{statement_type}] 계정명 없는 값 '{raw.strip()}' 무시 (pdf_page {lines[pos][0]})")
            pos += 1
            continue
        label_page = lines[pos][0]
        label_lines = [raw]
        pos += 1
        while pos < end and not is_value_token(lines[pos][1]):
            label_lines.append(lines[pos][1])
            pos += 1
        tokens: list[str] = []
        while pos < end and len(tokens) < n_values and is_value_token(lines[pos][1]):
            tokens.append(lines[pos][1])
            pos += 1

        label, indent, note_refs, inline_unit = split_label(label_lines)
        label_norm = re.sub(r"\s+", "", label)
        line = ParsedLine(
            line_no=len(parsed_lines) + 1,
            label_raw=label,
            label_norm=label_norm,
            indent=indent,
            is_total=bool(re.search(r"(총계|합계|소계)$", label_norm)),
            note_refs=note_refs,
            pdf_page=label_page,
            chunk_id=find_chunk_id(chunks_by_page, label_page, label_lines, tokens),
        )
        parsed_lines.append(line)

        if len(tokens) < n_values:
            warnings.append(
                f"[{statement_type}] '{label}' 값이 {len(tokens)}/{n_values}개뿐이라 값 저장 생략 (pdf_page {label_page})"
            )
            continue

        row_multiplier = multiplier
        if inline_unit:
            row_multiplier = UNIT_MULTIPLIERS[inline_unit]
        elif "주당" in label_norm or "(원)" in label_norm:
            row_multiplier = 1  # 주당순이익 등은 항상 원 단위

        if statement_type == "SCE":
            open_match = SCE_OPEN_RE.match(label)
            if open_match:
                opened = date(*(int(g) for g in open_match.groups()))
                for idx, period in enumerate(periods):
                    if period["start"] == opened:
                        block_period_idx = idx
                        break
                else:
                    warnings.append(f"[SCE] 기초일 {opened}에 맞는 기간이 없어 직전 기간으로 처리")

        for col_idx, token in enumerate(tokens):
            status, reported = parse_amount(token)
            if status == "unsupported":
                warnings.append(
                    f"[{statement_type}] '{label}' 값 '{token.strip()}'은 정수가 아니라 저장 생략 (pdf_page {label_page})"
                )
                continue
            line.values.append(
                ParsedValue(
                    period_idx=block_period_idx if statement_type == "SCE" else col_idx,
                    column_key=columns[col_idx],
                    raw=token.strip() or None,
                    status=status,
                    reported=reported,
                    amount=None if reported is None else reported * row_multiplier,
                    pdf_page=label_page,
                    chunk_id=line.chunk_id,
                )
            )

        if statement_type == "CF" and re.search(r"기말.*현금", label_norm):
            break  # 현금흐름표의 마지막 행. 이후는 주석 본문이다.

    pages_used = [title_page] + [ln.pdf_page for ln in parsed_lines]
    return ParsedStatement(
        statement_type=statement_type,
        title_raw=re.sub(r"\s+", " ", title_raw.strip()),
        unit_label_raw=unit_label,
        unit_multiplier=multiplier,
        periods=periods,
        lines=parsed_lines,
        pdf_page_start=min(pages_used),
        pdf_page_end=max(pages_used),
    )


def parse_statements(pages: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> ParseResult:
    """pages.json / chunks.json 내용에서 연결재무제표 표들을 파싱한다."""
    chunks_by_page: dict[int, list[dict[str, Any]]] = {}
    for chunk in chunks:  # 구간 제한 없음: 현금흐름표 뒷부분처럼 주석 구간 쪽에 실린 표도 찾아야 한다
        chunks_by_page.setdefault(chunk["pdf_page"], []).append(chunk)

    def find_starts(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
        return [
            (i, statement_type_of(raw))
            for i, (_, raw) in enumerate(lines)
            if statement_type_of(raw) and i + 1 < len(lines) and PERIOD_RE.search(lines[i + 1][1].strip())
        ]

    result = ParseResult()
    lines = collect_lines(pages)
    starts = find_starts(lines)
    if not starts:
        result.warnings.append("연결재무제표 표 제목을 찾지 못함")
        return result
    # 현금흐름표가 마지막 표이고 쪽을 넘어가면 뒷부분이 주석 첫 쪽에 실리는 경우가 있다.
    # (그 쪽은 section이 '주석'으로 붙어 있다.) 이때만 다음 쪽을 이어서 읽고,
    # 현금흐름표는 '기말 현금및현금성자산' 행에서 끝낸다.
    if starts[-1][1] == "CF":
        last_page = max(p["pdf_page"] for p in pages if p.get("section") == STATEMENT_SECTION)
        lines = collect_lines(pages, extra_pages=(last_page + 1,))
        starts = find_starts(lines)
    bounds = [s[0] for s in starts] + [len(lines)]
    for (start, statement_type), end in zip(starts, bounds[1:]):
        local_warnings: list[str] = []
        parsed = parse_statement(statement_type, lines, start, end, chunks_by_page, local_warnings)
        if statement_type == "SCE" and local_warnings:
            # 자본변동표는 회사마다 열 구성이 달라, 하나라도 어긋나면 잘못된 값이 들어가지 않도록 통째로 버린다.
            result.warnings.append(
                f"[SCE] 이 회사의 자본변동표 형식을 아직 지원하지 않아 건너뜀 (이상 {len(local_warnings)}건, 예: {local_warnings[0]})"
            )
            continue
        result.warnings.extend(local_warnings)
        if parsed:
            result.statements.append(parsed)
    return result


# ---------------------------------------------------------------------------
# 합계 검증 (파싱이 맞는지 확인하는 용도)
# ---------------------------------------------------------------------------
def _amount(statement: ParsedStatement, label_norm: str, period_idx: int, column_key: str = "") -> int | None:
    for line in statement.lines:
        if line.label_norm == label_norm:
            for value in line.values:
                if value.period_idx == period_idx and value.column_key == column_key:
                    return value.reported
    return None


def validate(result: ParseResult) -> list[tuple[str, bool | None, str]]:
    """(검사 이름, 통과 여부(None=검사 불가), 상세) 목록."""
    checks: list[tuple[str, bool | None, str]] = []
    by_type = {s.statement_type: s for s in result.statements}
    bs = by_type.get("BS")
    if bs:
        for idx, period in enumerate(bs.periods):
            assets = _amount(bs, "자산총계", idx)
            liabilities = _amount(bs, "부채총계", idx)
            equity = _amount(bs, "자본총계", idx)
            total = _amount(bs, "자본과부채총계", idx)
            name = f"BS {period['end']} 자산총계 = 부채총계 + 자본총계"
            if None in (assets, liabilities, equity):
                checks.append((name, None, "필요한 합계 행을 찾지 못함"))
            else:
                checks.append((name, assets == liabilities + equity, f"{assets:,} vs {liabilities + equity:,}"))
            if total is not None and assets is not None:
                checks.append((f"BS {period['end']} 자산총계 = 자본과부채총계", assets == total, f"{assets:,} vs {total:,}"))
    sce = by_type.get("SCE")
    if sce and bs:
        for idx, period in enumerate(sce.periods[:1]):
            closing = None
            for line in sce.lines:
                if "기말자본" in line.label_norm and line.values and line.values[0].period_idx == idx:
                    closing = next((v.reported for v in line.values if v.column_key == "자본 합계"), None)
            equity = _amount(bs, "자본총계", idx)
            name = f"SCE {period['end']} 기말자본 합계 = BS 자본총계"
            if closing is None or equity is None:
                checks.append((name, None, "행을 찾지 못함"))
            else:
                checks.append((name, closing == equity, f"{closing:,} vs {equity:,}"))
    cf = by_type.get("CF")
    if cf and bs:
        cash_bs = _amount(bs, "현금및현금성자산", 0)
        cash_cf = None
        for line in cf.lines:
            if line.label_norm in ("기말현금및현금성자산", "기말의현금및현금성자산"):
                cash_cf = next((v.reported for v in line.values if v.period_idx == 0), None)
        name = "CF 기말 현금및현금성자산 = BS 현금및현금성자산 (당기)"
        if cash_bs is None or cash_cf is None:
            checks.append((name, None, "행을 찾지 못함"))
        else:
            checks.append((name, cash_bs == cash_cf, f"{cash_cf:,} vs {cash_bs:,}"))
    return checks
