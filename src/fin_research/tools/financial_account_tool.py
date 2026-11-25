"""OpenDART 계정 테이블에서 의미와 기준이 확인된 재무 숫자만 추출한다."""

from decimal import Decimal, InvalidOperation

from fin_research.verify.accounts import load_aliases, normalize_name
from fin_research.tools.financial_models import FinancialContext, FinancialDataError, FinancialFact


# 계정 ID와 원문 이름은 같은 지표임이 명확한 것만 허용한다.
from fin_research.tools.financial_catalog import CATALOG

ACCOUNT_SPECS = {key: (item["ids"], tuple(item["names"]), item["statements"])
                 for key, item in CATALOG.items()}


def _amount(value) -> Decimal | None:
    """누락과 0을 구분하고 유한한 원 단위 금액만 반환한다."""
    if value is None or str(value).strip() in ("", "-", "－"):
        return None
    if isinstance(value, bool):
        raise FinancialDataError("금액이 숫자가 아닙니다.", "invalid_data")
    try:
        amount = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        raise FinancialDataError("DB 금액을 숫자로 해석하지 못했습니다.", "invalid_data") from None
    if not amount.is_finite():
        raise FinancialDataError("DB 금액이 유한하지 않습니다.", "invalid_data")
    return amount


class FinancialAccountTool:
    """동일 공시·연간·연결/별도의 계정값을 읽고 중복·개념 혼동을 검사한다."""

    def __init__(self, store):
        """읽기 전용 저장소를 주입해 실제 DB와 모의 DB에서 같은 처리를 사용한다."""
        self.store = store

    def lookup(self, context: FinancialContext, metrics: list[str], *, include_prior_equity: bool = False) -> list[FinancialFact]:
        """필요 계정만 읽고 ROE에 필요한 경우 전기말 자본도 같은 공시에서 확보한다."""
        if not metrics or any(metric not in ACCOUNT_SPECS for metric in metrics):
            raise ValueError("지원하는 재무 지표를 지정하세요.")
        shared = load_aliases()
        specs = {}
        for metric in metrics:
            ids, names, statements = ACCOUNT_SPECS[metric]
            alias = shared.get("operating_income" if metric == "operating_profit" else metric)
            specs[metric] = (tuple(dict.fromkeys(ids + (alias.account_ids if alias else ()))),
                             {normalize_name(name) for name in names} | (alias.names if alias else set()), statements)
        account_ids = sorted({identifier for metric in metrics for identifier in specs[metric][0]})
        names = sorted({name for metric in metrics for name in specs[metric][1]})
        result = self.store.read_financial_accounts(context, account_ids, names)
        if result.truncated:
            raise FinancialDataError("계정 조회 결과가 잘려 정확한 선택을 할 수 없습니다.", "truncated_data")
        facts = []
        for metric in metrics:
            ids, aliases, statements = specs[metric]
            candidates = [row for row in result.rows if row.get("sj_div") in statements
                          and (row.get("account_id") in ids or (
                              normalize_name(row.get("account_nm")) in aliases
                              and not str(row.get("account_id") or "").startswith(("ifrs-full_", "dart_"))
                          ))]
            # 명확한 표준 ID가 있으면 이름만 같은 사용자 정의 계정보다 우선한다.
            exact = [row for row in candidates if row.get("account_id") in ids]
            candidates = exact or candidates
            for row in candidates:
                if (row.get("corp_code") != context.corp_code or row.get("fiscal_year") != context.year
                        or row.get("fs_div") != ("CFS" if context.scope == "consolidated" else "OFS")
                        or row.get("rcept_no") != context.rcept_no or row.get("reprt_code") != "11011"
                        or row.get("currency") != context.currency):
                    raise FinancialDataError("공시 계정의 기업·연도·기준·통화가 다릅니다.", "context_mismatch")
            fields = [("thstrm_amount", context.year)]
            if metric == "total_equity" and include_prior_equity:
                fields.append(("frmtrm_amount", context.year - 1))
            for field, year in fields:
                available = [(row, _amount(row.get(field))) for row in candidates]
                available = [(row, amount) for row, amount in available if amount is not None]
                if not available:
                    continue
                if len({amount for _, amount in available}) != 1:
                    raise FinancialDataError(f"{metric}의 값이 서로 달라 확정할 수 없습니다.", "ambiguous_data")
                row, amount = sorted(available, key=lambda item: (item[0]["sj_div"] != "IS", item[0]["record_index"]))[0]
                facts.append(FinancialFact(
                    id=f"dart:{context.rcept_no}:{row['fs_div']}:{row['record_index']}:{field}",
                    metric=metric, value=amount, context=context.model_copy(update={"year": year}),
                    account_name=row["account_nm"], account_id=row.get("account_id") or "",
                    statement_type=row["sj_div"],
                ))
        return facts
