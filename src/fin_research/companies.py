"""분석 대상 기업 목록(configs/companies.yaml) 조회. clarify 노드·PDF 검증 데모 공용."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from fin_research.llm.client import PROJECT_ROOT

COMPANIES_PATH = PROJECT_ROOT / "configs" / "companies.yaml"

_LEGAL_SUFFIX = re.compile(r"\(주\)|㈜|주식회사")
_PUNCT = re.compile(r"[\s.,·\-_/]")


@lru_cache(maxsize=1)
def load_companies(path: Path = COMPANIES_PATH) -> tuple[dict, ...]:
    """companies.yaml의 companies 목록 (캐시)."""
    if not path.is_file():
        raise FileNotFoundError(f"기업 설정 파일이 없습니다: {path}")
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    companies = (config or {}).get("companies")
    if not isinstance(companies, list) or not companies:
        raise ValueError(f"companies.yaml에 'companies' 목록이 없습니다: {path}")
    return tuple(companies)


def normalize_name(text: str) -> str:
    """비교용 이름: 법인 표기·공백·구두점 제거, 대소문자 무시. 예: '(주)JYP Ent.' → 'jypent'"""
    return _PUNCT.sub("", _LEGAL_SUFFIX.sub("", text)).casefold()


def available_years(company: dict) -> list[int]:
    """수집 설정된 사업연도 (reports의 키)."""
    return sorted(int(year) for year in (company.get("reports") or {}))


def supported_names() -> list[str]:
    return [c["name"] for c in load_companies()]


def find_companies(mention: str) -> list[dict]:
    """회사 언급 → 후보 기업. 0개=없음, 1개=확정, 2개 이상=모호.

    ① 이름 정확히 일치 또는 corp_code 일치 → 바로 반환 ('에코프로' ≠ '에코프로비엠')
    ② 언급이 이름의 일부 ('하이닉스' → SK하이닉스, 'SK' → SK하이닉스·SK텔레콤)
    ③ 이름이 언급의 일부 ('에코프로비엠의' → 가장 긴 이름만)
    """
    key = normalize_name(mention)
    if not key:
        return []
    companies = load_companies()
    names = {id(c): normalize_name(c["name"]) for c in companies}

    exact = [c for c in companies if names[id(c)] == key or c.get("corp_code") == mention.strip()]
    if exact:
        return exact

    partial = [c for c in companies if key in names[id(c)]]
    if partial:
        return partial

    contained = [c for c in companies if names[id(c)] in key]
    if contained:
        longest = max(len(names[id(c)]) for c in contained)
        return [c for c in contained if len(names[id(c)]) == longest]
    return []
