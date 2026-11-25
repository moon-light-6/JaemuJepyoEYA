"""LLM이 쓴 문장의 숫자·출처를 코드로 검사하는 도우미 (researcher·writer 공용).

원칙: LLM은 읽고 쓰기만, 숫자가 입력에 있는지는 코드가 확인한다 (로컬 LLM 대비).
"""
from __future__ import annotations

import re

# 영문자 바로 뒤 숫자('갤럭시 S25', 'Q3', 'S1')는 이름·번호라 금액으로 보지 않는다
_NUMBER = re.compile(r"(?<![A-Za-z\d])\d[\d,]*(?:\.\d+)?")
_CITATION = re.compile(r"\[([^\[\]]*?\bS\d+\b[^\[\]]*?)\]")
# 장부 번호(S3)와 다중 도구 조사의 임시 번호(profitability.r1.q1.S3)를 통째로 읽는다
_SOURCE_ID = re.compile(r"(?<![\w.])(?:[a-z_]+\.r\d+\.q\d+\.)?S\d+\b")
# 문장 끝(. ! ? 또는 인용 ']') 뒤 공백에서 나눈다. '입니다. [S1]'처럼 뒤따르는 인용은 앞 문장에 붙인다.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?\]])\s+(?!\[)")


def numbers_in(text: str) -> set[str]:
    """비교용 숫자: 쉼표를 빼고, 한 자리 숫자('2문장', '3개')는 제외한다."""
    found = set()
    for raw in _NUMBER.findall(text):
        number = raw.replace(",", "").rstrip(".")
        if len(number.replace(".", "")) >= 2:
            found.add(number)
    return found


def citations_in(text: str) -> set[str]:
    """[S1]·[S1, S3] 안의 번호만 ('갤럭시 S25' 같은 제품명 제외)."""
    return {i for group in _CITATION.findall(text) for i in _SOURCE_ID.findall(group)}


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_BREAK.split(text.strip()) if s]


def drop_unsupported_numbers(answer: str, sources: list[str]) -> tuple[str, list[str]]:
    """근거 원문(sources)에 없는 숫자가 든 문장을 뺀다. (남은 답변, 뺀 숫자들).

    예: 원문에 49건·13건만 있는데 '총 62건'이라고 더해 쓴 문장은 빠진다.
    """
    allowed = set().union(*(numbers_in(s) for s in sources)) if sources else set()
    kept, dropped = [], []
    for sentence in split_sentences(answer):
        extra = numbers_in(sentence) - allowed
        if extra:
            dropped.extend(sorted(extra))
        else:
            kept.append(sentence)
    return " ".join(kept), list(dict.fromkeys(dropped))
