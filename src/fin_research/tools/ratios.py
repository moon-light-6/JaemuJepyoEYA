"""재무비율의 공통 계산식.

모든 반환값은 퍼센트 단위의 Decimal이다(예: Decimal('20')는 20%).
표시용 반올림은 호출부에서 수행하며 계산은 유효숫자 50자리로 수행한다.
입력은 동일한 회사·연결/별도 기준·통화·금액 단위를 사용해야 한다.
기간과 출처의 일치 여부는 이 숫자 계산 모듈이 아닌 호출부에서 검증한다.
"""

from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import TypeAlias


Amount: TypeAlias = Decimal | int | str

__all__ = [
    "UndefinedRatioError",
    "debt_ratio",
    "current_ratio",
    "return_on_equity",
    "operating_profit_margin",
]


class UndefinedRatioError(ValueError):
    """분모가 0 이하이므로 유의미한 재무비율을 계산할 수 없음."""


def _amount(value: Amount, name: str) -> Decimal:
    """단위·쉼표가 없는 유한한 숫자만 허용하고 이진 부동소수점은 거부한다."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{name}: Decimal, int 또는 숫자 문자열이 필요합니다.")
    if isinstance(value, str) and (not value.strip() or "," in value or "_" in value):
        raise ValueError(f"{name}: 단위·쉼표 없는 숫자 문자열이 필요합니다.")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name}: 올바른 숫자가 아닙니다.") from exc
    if not number.is_finite():
        raise ValueError(f"{name}: NaN이나 무한대는 사용할 수 없습니다.")
    return number


def _positive_denominator(value: Decimal, name: str) -> Decimal:
    if value <= 0:
        raise UndefinedRatioError(f"{name}: 분모는 0보다 커야 합니다.")
    return value


def _percent(numerator: Decimal, denominator: Decimal) -> Decimal:
    # 호출부의 Decimal 정밀도·반올림 설정이 결과에 영향을 주지 않도록 분리한다.
    with localcontext(Context(prec=50)):
        return numerator * 100 / denominator


def debt_ratio(total_liabilities: Amount, total_equity: Amount) -> Decimal:
    """부채비율(%) = 부채총계 / 자본총계 × 100. 부채총계는 0 이상이어야 한다."""
    liabilities = _amount(total_liabilities, "부채총계")
    equity = _positive_denominator(_amount(total_equity, "자본총계"), "자본총계")
    if liabilities < 0:
        raise ValueError("부채총계는 0 이상이어야 합니다.")
    return _percent(liabilities, equity)


def current_ratio(current_assets: Amount, current_liabilities: Amount) -> Decimal:
    """유동비율(%) = 유동자산 / 유동부채 × 100. 유동자산은 0 이상이어야 한다."""
    assets = _amount(current_assets, "유동자산")
    liabilities = _positive_denominator(_amount(current_liabilities, "유동부채"), "유동부채")
    if assets < 0:
        raise ValueError("유동자산은 0 이상이어야 합니다.")
    return _percent(assets, liabilities)


def return_on_equity(
    net_income: Amount, opening_equity: Amount, closing_equity: Amount
) -> Decimal:
    """ROE(%) = 당기순이익 / ((기초자본 + 기말자본) / 2) × 100.

    기초·기말 자본이 모두 필요하며 평균 자본이 0 이하면 계산하지 않는다.
    손실에 따른 음수 ROE는 허용한다. 분기·반기 이익을 자동 연율화하지 않는다.
    연결 기준에서 전체 순이익을 사용하면 자본총계를, 지배주주순이익을 사용하면
    지배주주지분을 기초·기말에 일관되게 사용해야 한다.
    """
    income = _amount(net_income, "당기순이익")
    opening = _amount(opening_equity, "기초자본")
    closing = _amount(closing_equity, "기말자본")
    with localcontext(Context(prec=50)):
        average = _positive_denominator((opening + closing) / 2, "평균자본")
        return _percent(income, average)


def operating_profit_margin(operating_profit: Amount, revenue: Amount) -> Decimal:
    """영업이익률(%) = 영업이익 / 매출액 × 100. 영업손실은 음수로 반환한다."""
    profit = _amount(operating_profit, "영업이익")
    sales = _positive_denominator(_amount(revenue, "매출액"), "매출액")
    return _percent(profit, sales)
