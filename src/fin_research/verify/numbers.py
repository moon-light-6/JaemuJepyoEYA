"""금액 파싱·단위 환산·허용 오차 비교. LLM을 쓰지 않는다.
- 주장 값(원문 단위)과 DB 값(원 단위)을 받아 같은지 다른지만 판단한다.
- not_found·unverifiable은 여기서 정하지 않는다 (verify_claim 노드가 정한다).
- state.py에 의존하지 않는다. 결과는 Comparison으로 돌려주고 노드가 Verdict로 옮긴다.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Literal, NamedTuple

# 단위

UNIT_SCALE: dict[str, int] = {
    "원": 1,
    "천원": 10**3,
    "만원": 10**4,
    "백만원": 10**6,
    "억원": 10**8,
    "십억원": 10**9,
    "조원": 10**12,
}

PERCENT = "%"
_PERCENT_ALIASES = {"%", "퍼센트", "%p", "퍼센트포인트"}
_UNIT_ALIASES = {"krw": "원", "won": "원"}

# 값이 없다는 표기 (DART 표의 빈 칸)
_EMPTY = {"", "-", "－", "—", "–"}
# 음수 표기: △1,234 / ▲1,234 / -1,234 / (1,234)
_NEGATIVE_MARKS = ("△", "▲", "-", "－", "−")

def normalize_unit(unit:str|None)->str|None:
    """'조', '조 원', 'KRW' 같은 표기를 UNIT_SCALE의 키나 '%'로 맞춘다. 모르면 None."""
    if unit is None:
        return None
    key = re.sub(r"\s+", "", unit).lower()

    if not key: 
        return None
    if key in _PERCENT_ALIASES:
        return PERCENT
    key=_UNIT_ALIASES.get(key,key)

    if key in UNIT_SCALE:
        return key

    if key+"원" in UNIT_SCALE:
        return key+'원'
    return None

# 숫자 일기

def parse_number(text:str|None)-> float|None:
    """'1,234', '△1,234', '(1,234)', '12.5%' → float. 값이 없거나 숫자가 아니면 None."""
    if text is None: 
        return None
    raw=re.sub(r"[\s,%]", "", str(text))
    if raw in _EMPTY:
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative, raw = True, raw[1:-1]
    elif raw.startswith(_NEGATIVE_MARKS):
        negative, raw = True, raw[1:]
    try:
        number = float(raw)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return -number if negative else number

_KOREAN_PART = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(조|억|만)")
_KOREAN_SCALE = {"조": 10**12, "억": 10**8, "만": 10**4}


def parse_korean_amount(text: str | None) -> float | None:
    """'32조 7,260억 원', '2.2조원', '5,840억' → 원 단위. 조·억·만이 없으면 None.

    '1억 2천만'처럼 천·백이 붙은 표기는 지원하지 않는다 (None이 아니라 일부만 읽힐 수 있어
    호출하는 쪽에서 LLM 값과 교차 확인한다).
    """
    if not text:
        return None
    parts = _KOREAN_PART.findall(text)
    if not parts:
        return None
    total = 0.0
    for number, unit in parts:
        value = parse_number(number)
        if value is None:
            return None
        total += value * _KOREAN_SCALE[unit]
    first = text.strip()
    negative = first.startswith(_NEGATIVE_MARKS) or (first.startswith("(") and first.endswith(")"))
    return -total if negative else total


# 환산 

def to_won(value: float | None, unit: str | None) -> float | None:
    """(2.2, '조원') → 2.2e12. 비율(%)은 그대로, 모르는 단위나 값 없음은 None."""
    if value is None:
        return None
    canonical = normalize_unit(unit)
    if canonical is None:
        return None
    if canonical == PERCENT:
        return float(value)
    return float(value) * UNIT_SCALE[canonical]


def decimal_places(value: float | str) -> int:
    """표시된 소수점 아래 자릿수. float 오차를 피하려고 문자열·Decimal로 센다.

    - 문자열은 적힌 그대로 센다: '2.20' → 2 (뒤의 0도 정밀도다)
    - float는 JSON에서 온 값이라 2200 → 2200.0이 된다: 정수면 0, 아니면 repr로 센다
    """
    if isinstance(value, float):
        if value.is_integer():
            return 0
        text = repr(value)
    else:
        text = re.sub(r"[\s,%]", "", str(value)).lstrip("△▲-－−(").rstrip(")")
    try:
        exponent = Decimal(text).as_tuple().exponent
    except InvalidOperation:
        return 0
    return max(0, -exponent) if isinstance(exponent, int) else 0


def rounding_tolerance(value: float | str, unit: str | None) -> float | None:
    """반올림 허용 오차. 2.2조원 → 0.05조원 = 5e10원, 120% → 0.5%p."""
    canonical = normalize_unit(unit)
    if canonical is None:
        return None
    half_step = 0.5 * 10 ** (-decimal_places(value))
    return half_step if canonical == PERCENT else half_step * UNIT_SCALE[canonical]

# 비교

class Comparison(NamedTuple):
    label: Literal["match", "mismatch"]
    claim_won: float
    db_won: float
    diff_ratio: float | None  # (claim - db) / |db|, db가 0이면 None
    tolerance_won: float


def relative_diff(claim: float, db: float) -> float | None:
    return None if db == 0 else (claim - db) / abs(db)


def compare(
    claim_value: float | str,
    claim_unit: str | None,
    db_value: float,
    *,
    min_tolerance: float = 0.0,
) -> Comparison | None:
    """주장 값(원문 단위)과 DB 값(원 단위, 비율은 %)을 비교한다.

    허용 오차는 주장이 표시한 정밀도에서 정한다 (2.2조 → ±0.05조).
    claim_value에 원문 문자열('2.20')을 주면 자릿수를 더 정확히 센다.
    단위를 모르거나 값을 못 읽으면 None → 노드가 unverifiable로 처리한다.
    """
    number = parse_number(claim_value) if isinstance(claim_value, str) else claim_value
    claim_won = to_won(number, claim_unit)
    tolerance = rounding_tolerance(claim_value, claim_unit)
    if claim_won is None or tolerance is None:
        return None
    tolerance = max(tolerance, min_tolerance)
    db = float(db_value)
    diff = claim_won - db
    opposite_sign = claim_won * db < 0
    # 부동소수점 오차(2.2 * 1e12 = 2200000000000.0002)만큼만 여유를 둔다.
    # float 상대 정밀도는 약 1e-16이라 1e-12면 충분하다 (800조 원에서도 약 800원).
    epsilon = 1e-12 * max(abs(claim_won), abs(db), 1.0)
    label = "match" if not opposite_sign and abs(diff) <= tolerance + epsilon else "mismatch"
    return Comparison(label, claim_won, db, relative_diff(claim_won, db), tolerance)