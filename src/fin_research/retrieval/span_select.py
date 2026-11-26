"""검색으로 찾은 청크에서 질문·답변에 맞는 짧은 근거 조각을 고른다 (2단계 근거, 1단계: 보여 주기용).

색인·청킹은 그대로 두고, 검색 결과로 나온 청크만 그 자리에서 잘게 나눈다.
  1) 청크 → 조각: 주석 항목 기호(가. 나. … 사.), 표의 행(머리글·단위를 붙여서), 문장
  2) 조각 점수: 답변에 나온 숫자가 들어 있는가(가장 강한 신호) + 질문·답변과 글자 겹침
LLM·임베딩을 쓰지 않아 빠르고, 같은 입력이면 항상 같은 조각을 고른다.
조각은 원문을 자르기만 하므로 원문에 없는 글자가 들어가지 않는다.
"""
from __future__ import annotations

import re
from typing import NamedTuple

# 청크 앞의 검색 문맥 머리글: "[NAVER | 2025년 연결 | 재무상태표 | 단위: 원 | 열: …]"
_HEADER = re.compile(r"^\s*\[([^\[\]\n]{1,300})\]\s*")
_UNCLOSED_HEADER = re.compile(r"^\s*\[검색 문맥(?:\s*\|\s*[^|\]\n]{1,40}?)*\s*\|\s*")
# 주석 항목 기호. 앞 글자가 문장 끝(.)·공백·머리글 끝(])일 때만 새 항목으로 본다 ("다만"은 제외)
_ITEM_MARKS = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
_ITEM_SPLIT = re.compile(rf"(?:(?<=[.\]\s])|^)(?=[{_ITEM_MARKS}]\.\s?\S)")
_SENTENCE_SPLIT = re.compile(r"(?<=[다요]\.)\s+|(?<=니다\.)(?=\S)")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+|\d{4,}")  # 금액 모양 (열 이름의 '제 27 기'는 제외)
_UNIT_LINE = re.compile(r"단위\s*[:：]")

MIN_SPAN = 30  # 이보다 짧은 조각은 다음 조각과 합친다 (주어가 잘린 조각 방지)


class Span(NamedTuple):
    text: str  # 보여 줄 조각 (표는 머리글 포함)
    kind: str  # item / row / sentence / whole


def split_header(text: str) -> tuple[str, str]:
    """(검색 문맥 머리글, 본문). 머리글이 없으면 ("", 원문).

    청크가 문단 중간에서 잘려 머리글의 ']'가 본문 끝으로 밀린 경우('[검색 문맥 | 주석 | 32. 우발채무…')는
    앞의 '[검색 문맥 | … | ' 부분만 떼어 낸다.
    """
    match = _HEADER.match(text)
    # 머리글은 '기업 | 연도 | 표 이름 | 단위 | 열' 같은 짧은 칸들이다. 긴 칸이 있으면 본문이 갇힌 것
    if match and "|" in match.group(1) and all(len(part.strip()) <= 60 for part in match.group(1).split("|")):
        return match.group(0).strip(), text[match.end():]
    unclosed = _UNCLOSED_HEADER.match(text)
    if unclosed:
        return "", text[unclosed.end():].rstrip().rstrip("]")
    return "", text


def _merge_short(parts: list[str]) -> list[str]:
    merged: list[str] = []
    for part in (p.strip() for p in parts):
        if not part:
            continue
        if merged and len(merged[-1]) < MIN_SPAN:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def _table_spans(header: str, body: str) -> list[Span] | None:
    """'a | b | c' 행이 여럿이면 표. 숫자가 있는 행마다 단위·열 이름을 붙인 조각을 만든다."""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    rows = [line for line in lines if "|" in line]
    if len(rows) < 2:
        return None
    context = [header] if header else []
    if not header:  # 머리글이 없으면 단위 줄과 첫 열 이름 줄을 대신 붙인다
        context += [line for line in lines if _UNIT_LINE.search(line)][:1]
        context += [line for line in rows if not _AMOUNT.search(line)][:1]
    prefix = "\n".join(context)
    spans = [Span(f"{prefix}\n{row}" if prefix else row, "row") for row in rows if _AMOUNT.search(row)]
    # 표 앞뒤의 서술 문장(예: '사. … 소송가액 …')도 조각으로 남긴다
    prose = " ".join(line for line in lines if "|" not in line and not _UNIT_LINE.search(line))
    spans += [Span(p, "item") for p in _merge_short(_ITEM_SPLIT.split(prose)) if _NUMBER.search(p) or len(p) > MIN_SPAN]
    return spans or None


def split_spans(text: str) -> list[Span]:
    """청크 → 조각 목록. 나눌 기준이 없으면 청크 전체 하나."""
    header, body = split_header(text)
    body = body.strip()
    if not body:
        return [Span(text.strip(), "whole")]
    table = _table_spans(header, body)
    if table:
        return table
    flat = " ".join(body.split())
    items = _merge_short(_ITEM_SPLIT.split(flat))
    if len(items) > 1:
        return [Span(item, "item") for item in items]
    sentences = _merge_short(_SENTENCE_SPLIT.split(flat))
    if len(sentences) > 1:
        return [Span(s, "sentence") for s in sentences]
    return [Span(flat, "whole")]


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"[\s|,.\[\]()]", "", text)
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def _numbers(text: str) -> set[str]:
    """비교용 숫자 (쉼표 제거, 두 자리 이상). 연도처럼 흔한 값도 남지만 겹침 점수가 함께 판단한다."""
    return {n.replace(",", "") for n in _NUMBER.findall(text) if len(n.replace(",", "")) >= 2}


def score_span(span: str, question: str, answer: str = "") -> float:
    """답변 숫자가 조각에 있으면 큰 점수, 그다음 질문·답변과의 글자(2글자 묶음) 겹침."""
    answer_numbers = _numbers(answer) - _numbers(question)  # 질문에 있던 연도 등은 빼고
    number_hits = len(answer_numbers & _numbers(span))
    query = _bigrams(f"{question} {answer}")
    overlap = len(query & _bigrams(span)) / (len(query) or 1)
    return number_hits * 10 + overlap


def select_quote(text: str, question: str, answer: str = "", max_chars: int = 300) -> str | None:
    """청크에서 가장 맞는 조각 하나. 청크가 이미 짧으면 None (원문 그대로 보면 된다)."""
    if not text or len(" ".join(text.split())) <= max_chars:
        return None
    spans = split_spans(text)
    best = max(spans, key=lambda s: score_span(s.text, question, answer))
    quote = best.text
    if len(quote) > max_chars:  # 조각도 길면 자른다 (원문의 앞부분만, 글자를 바꾸지 않는다)
        quote = quote[:max_chars].rstrip() + "…"
    return quote
