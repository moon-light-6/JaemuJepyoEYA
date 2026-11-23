"""Supabase fin.dart_financial_accounts에서 검증 기준값을 조회한다.

흐름: Claim.item('영업이익') → account_aliases.yaml → DB 행 고르기 → AccountValue(원 단위)

- DB 조회(fetch_rows)와 행 고르기(select_value)를 나눠, 고르기 규칙은 DB 없이 테스트한다.
- 기업·연도·연결별도마다 한 번만 조회해 캐시한다 (AccountStore). 주장이 20개여도 쿼리는 1번.
- 팀 공용 DB라 읽기 전용으로 연결하고, 값은 항상 %s 자리표시자로 넘긴다.
- state.py에 의존하지 않는다. 판정(not_found/unverifiable)은 verify_claim 노드가 정한다.
"""
from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ALIASES_PATH = PROJECT_ROOT / "configs" / "account_aliases.yaml"

# 자본변동표(SCE)는 같은 계정이 자본 구성요소별로 여러 행이라 제외한다.
# 같은 계정이 여러 표에 있으면 이 순서로 고른다 (당기순이익: IS·CIS·CF에 모두 있음).
STATEMENT_PRIORITY: tuple[str, ...] = ("IS", "CIS", "BS", "CF")
SCOPE_TO_FS_DIV = {"consolidated": "CFS", "separate": "OFS", "unknown": "CFS"}
PERIOD_COLUMN = {"current": "thstrm_amount", "prior": "frmtrm_amount", "unknown": "thstrm_amount"}

SQL_ROWS = """
SELECT rcept_no, fs_div, sj_div, account_id, account_nm, account_detail,
       thstrm_amount, frmtrm_amount, ord
FROM fin.dart_financial_accounts
WHERE corp_code = %s AND fiscal_year = %s AND fs_div = %s
"""

Row = dict[str, Any]


# ----------------------
# 결과 형태
# ----------------------

class AccountValue(NamedTuple):
    amount: float  # 원 단위 (period에 맞는 당기/전기 값)
    account_id: str | None
    account_nm: str
    sj_div: str  # BS / IS / CIS / CF
    rcept_no: str
    fs_div: str  # CFS / OFS
    period: str  # current / prior


class AliasSpec(NamedTuple):
    key: str  # 예: operating_income
    label: str  # 예: 영업이익
    names: frozenset[str]  # 정규화된 이름
    account_ids: tuple[str, ...]


# ----------------------
# 항목 이름 → 별칭
# ----------------------

_NAME_NOISE = re.compile(r"[\s()（）\[\]·ㆍ,]")


def normalize_name(name: str | None) -> str:
    """'영업이익 (손실)' → '영업이익손실', '자산 총계' → '자산총계'."""
    return _NAME_NOISE.sub("", str(name or "")).lower()


@lru_cache(maxsize=4)
def load_aliases(path: Path = ALIASES_PATH) -> dict[str, AliasSpec]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    items = data.get("items")
    if not isinstance(items, dict) or not items:
        raise ValueError(f"{path}: items가 비어 있습니다.")
    specs: dict[str, AliasSpec] = {}
    seen: dict[str, str] = {}
    for key, item in items.items():
        names = frozenset(normalize_name(n) for n in item.get("names", []) if n)
        account_ids = tuple(item.get("account_ids", []))
        if not names or not account_ids:
            raise ValueError(f"{path}: {key}에 names와 account_ids가 모두 있어야 합니다.")
        for name in names:
            # 한 이름이 두 항목에 있으면 어느 쪽인지 정할 수 없다
            if name in seen:
                raise ValueError(f"{path}: 이름 '{name}'이 {seen[name]}과 {key}에 중복됩니다.")
            seen[name] = key
        specs[key] = AliasSpec(key, item.get("label", key), names, account_ids)
    return specs


def find_alias(item: str | None, aliases: dict[str, AliasSpec] | None = None) -> AliasSpec | None:
    """정확히 같은 이름만 일치로 본다 ('매출'은 '매출원가'와 일치하지 않는다)."""
    key = normalize_name(item)
    if not key:
        return None
    for spec in (aliases or load_aliases()).values():
        if key in spec.names:
            return spec
    return None


# ----------------------
# 행 고르기 (DB 없이 테스트)
# ----------------------

def _matching_rows(rows: list[Row], spec: AliasSpec) -> list[Row]:
    main = [r for r in rows if r.get("sj_div") in STATEMENT_PRIORITY]
    # 표준 코드가 가장 정확하다. 우선순위가 앞인 코드부터 찾는다.
    for account_id in spec.account_ids:
        by_id = [r for r in main if r.get("account_id") == account_id]
        if by_id:
            return by_id
    # '-표준계정코드 미사용-' 행 대비: 계정명으로 찾는다
    return [r for r in main if normalize_name(r.get("account_nm")) in spec.names]


def _sort_key(row: Row) -> tuple[int, int]:
    order = row.get("ord")
    return STATEMENT_PRIORITY.index(row["sj_div"]), order if isinstance(order, int) else 10**9


def select_value(rows: list[Row], spec: AliasSpec, period: str = "current") -> AccountValue | None:
    """규칙: 본표만 → account_id → 계정명 → IS·CIS·BS·CF 순 → ord가 작은 행."""
    if period not in PERIOD_COLUMN:
        raise ValueError(f"period는 {sorted(PERIOD_COLUMN)} 중 하나여야 합니다: {period!r}")
    candidates = _matching_rows(rows, spec)
    if not candidates:
        return None
    row = min(candidates, key=_sort_key)
    amount = row.get(PERIOD_COLUMN[period])
    if amount is None:
        return None
    return AccountValue(
        amount=float(amount),
        account_id=row.get("account_id"),
        account_nm=str(row.get("account_nm")),
        sj_div=str(row["sj_div"]),
        rcept_no=str(row.get("rcept_no")),
        fs_div=str(row.get("fs_div")),
        period="prior" if period == "prior" else "current",
    )


# ----------------------
# DB 조회
# ----------------------

def fetch_rows(corp_code: str, fiscal_year: int, fs_div: str) -> list[Row]:
    """한 기업·연도·연결별도의 행을 전부 가져온다 (기업당 200~350행)."""
    import psycopg2  # 테스트·다른 모듈이 DB 드라이버 없이도 이 파일을 불러올 수 있게

    load_dotenv(PROJECT_ROOT / ".env")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError(".env에 DATABASE_URL이 없습니다.")
    conn = psycopg2.connect(database_url, connect_timeout=8)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(SQL_ROWS, (corp_code, fiscal_year, fs_div))
            columns = [d[0] for d in cur.description]
            rows = [dict(zip(columns, values)) for values in cur.fetchall()]
    finally:
        conn.close()
    for row in rows:
        for column in ("thstrm_amount", "frmtrm_amount"):
            if row[column] is not None:
                row[column] = float(row[column])  # Decimal → float
    return rows


class AccountStore:
    """기업·연도·연결별도마다 한 번만 조회해 두는 캐시. 병렬 verify_claim이 같이 쓴다."""

    def __init__(self, fetch: Callable[[str, int, str], list[Row]] = fetch_rows,
                 aliases: dict[str, AliasSpec] | None = None):
        self._fetch = fetch
        self._aliases = aliases
        self._cache: dict[tuple[str, int, str], list[Row]] = {}
        self._lock = threading.Lock()

    def rows(self, corp_code: str, fiscal_year: int, fs_div: str) -> list[Row]:
        key = (corp_code, int(fiscal_year), fs_div)
        with self._lock:
            if key not in self._cache:
                self._cache[key] = self._fetch(*key)
            return self._cache[key]

    def has_company_data(self, corp_code: str, fiscal_year: int, scope: str = "consolidated") -> bool:
        """행이 하나도 없으면 데이터 문제(not_found), 있는데 값이 없으면 항목 문제(unverifiable)."""
        return bool(self.rows(corp_code, fiscal_year, SCOPE_TO_FS_DIV[scope]))

    def lookup(self, corp_code: str, fiscal_year: int, item: str,
               scope: str = "consolidated", period: str = "current") -> AccountValue | None:
        spec = find_alias(item, self._aliases)
        if spec is None:
            return None
        if scope not in SCOPE_TO_FS_DIV:
            raise ValueError(f"scope는 {sorted(SCOPE_TO_FS_DIV)} 중 하나여야 합니다: {scope!r}")
        return select_value(self.rows(corp_code, fiscal_year, SCOPE_TO_FS_DIV[scope]), spec, period)
