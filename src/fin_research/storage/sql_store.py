"""재무 조회용 PostgreSQL 뷰에 대한 제한된 읽기 전용 실행기."""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext

import psycopg2
from sqlglot import exp, parse
from sqlglot.errors import ParseError, UnsupportedError


# 스키마 변경 시 조회 계약도 함께 갱신한다. 원본 테이블은 모델에 노출하지 않는다.
VIEW_COLUMNS = {
    "v_fin_value": (
        "corp_code stock_code company_name rcept_no fiscal_year fs_scope statement_type "
        "period_role term_no period_start period_end line_no account_name_raw "
        "account_name_norm std_account_code column_key amount_raw amount_as_reported "
        "amount unit_multiplier value_status pdf_page chunk_id"
    ).split(),
    "v_fin_std_value": (
        "corp_code company_name rcept_no fiscal_year fs_scope period_role period_end "
        "std_account_code std_account_name amount pdf_page chunk_id"
    ).split(),
    "v_fin_ratio": (
        "corp_code company_name rcept_no fiscal_year fs_scope period_role period_end "
        "debt_ratio_pct current_ratio_pct equity_ratio_pct gross_margin_pct "
        "operating_margin_pct net_margin_pct"
    ).split(),
}

# 임의 함수·명령·연산자를 차단하기 위해 지원하는 구문만 허용한다.
_ALLOWED_NODES = {
    "Select", "From", "Table", "Column", "Identifier", "Literal", "Star", "Alias",
    "Where", "And", "Or", "Not", "EQ", "NEQ", "LT", "LTE", "GT", "GTE",
    "Is", "In", "Between", "Like", "ILike", "Paren", "Subquery", "Join",
    "Group", "Having", "Order", "Ordered", "Limit", "Offset", "Distinct",
    "Add", "Sub", "Mul", "Div", "Mod", "Neg", "Case", "If", "Boolean", "Null",
    "TableAlias", "Cast", "DataType", "DataTypeParam", "Filter",
    "Count", "Max", "Min", "Sum", "Avg", "Round", "Abs", "Coalesce", "Nullif",
    "Lower", "Upper", "Trim", "Length",
}
_ALLOWED_TYPES = {
    "TEXT", "VARCHAR", "CHAR", "INT", "BIGINT", "SMALLINT", "DECIMAL",
    "DOUBLE", "FLOAT", "BOOLEAN", "DATE", "TIMESTAMP", "TIMESTAMPTZ",
}


class UnsafeSQLError(ValueError):
    """지원 범위 밖이거나 쓰기를 유발하는 SQL."""


class SQLExecutionError(RuntimeError):
    """접속 정보를 제외하고 호출부에 반환하는 DB 실행 오류."""


def validate_readonly_sql(sql: str) -> str:
    """단일 SELECT와 허용된 fin 뷰 및 수식만 승인한다."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 20000:
        raise UnsafeSQLError("SQL은 비어 있지 않은 20,000자 이내 문자열이어야 합니다.")
    try:
        statements = parse(sql, read="postgres")
    except (ParseError, UnsupportedError, RecursionError):
        raise UnsafeSQLError("PostgreSQL SQL로 분석할 수 없습니다.") from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise UnsafeSQLError("단일 SELECT 문만 실행할 수 있습니다.")
    tree = statements[0]
    for node in tree.walk():
        if type(node).__name__ not in _ALLOWED_NODES:
            raise UnsafeSQLError(f"허용되지 않은 SQL 구문입니다: {type(node).__name__}")
        if isinstance(node, exp.DataType) and node.this.value not in _ALLOWED_TYPES:
            raise UnsafeSQLError("이 자료형으로의 변환은 허용되지 않습니다.")
        if isinstance(node, exp.Table):
            if node.catalog or node.db != "fin" or node.name not in VIEW_COLUMNS:
                raise UnsafeSQLError("fin의 재무 조회용 뷰 3개만 참조할 수 있습니다.")
    if not list(tree.find_all(exp.Table)):
        raise UnsafeSQLError("재무 조회용 뷰를 지정하세요.")
    return tree.sql(dialect="postgres")


def schema_description() -> str:
    """모델에 전달할 읽기 전용 스키마 계약."""
    return "\n".join(
        f"fin.{name}({', '.join(columns)})" for name, columns in VIEW_COLUMNS.items()
    )


@dataclass(frozen=True)
class SQLResult:
    sql: str
    columns: list[str]
    rows: list[dict]
    row_count: int
    truncated: bool

    def to_dict(self) -> dict:
        result = asdict(self)
        # DB 원본 금액은 계산용으로 유지하고, 응답에만 표시용 값을 추가한다.
        if "amount" in self.columns and "amount_display" not in self.columns:
            result["columns"].append("amount_display")
            for row in result["rows"]:
                row["amount_display"] = format_krw(row.get("amount"))
        return result


def format_krw(amount: int | Decimal | None) -> str | None:
    """원 단위 금액을 조·억·만 단위로 소수 셋째 자리까지 반올림해 표시한다."""
    if isinstance(amount, bool) or not isinstance(amount, (int, Decimal)):
        return None
    number = Decimal(amount)
    if not number.is_finite():
        return None
    with localcontext(Context(prec=50, rounding=ROUND_HALF_UP)):
        for divisor, label in ((10**12, "조"), (10**8, "억"), (10**4, "만")):
            if abs(number) >= divisor:
                scaled = (number / divisor).quantize(Decimal("0.001"))
                return f"약 {scaled:,.3f}{label} 원"
        # 작은 금액은 단위 변환이나 반올림 없이 표시한다.
        text = format(number, ",f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text}원"


def json_value(value):
    """금액 정밀도를 유지하고 날짜도 JSON으로 출력한다."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"JSON으로 변환할 수 없는 자료형입니다: {type(value).__name__}")


class PostgresReadOnlyStore:
    """연결마다 읽기 전용 트랜잭션과 시간·행 수 상한을 설정한다."""

    def __init__(self, database_url: str | None = None, *, max_rows: int = 100,
                 timeout_ms: int = 10000):
        if type(max_rows) is not int or not 1 <= max_rows <= 1000:
            raise ValueError("max_rows는 1~1000 정수여야 합니다.")
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 60000:
            raise ValueError("timeout_ms는 100~60000 정수여야 합니다.")
        self._database_url = database_url or os.getenv("DATABASE_READONLY_URL") or os.getenv("DATABASE_URL")
        if not self._database_url:
            raise ValueError(".env에 DATABASE_READONLY_URL 또는 DATABASE_URL을 설정하세요.")
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms

    def execute(self, sql: str) -> SQLResult:
        """모델 생성 SELECT를 검사하고 리터럴의 %를 DB 매개변수 문법과 구분한다."""
        normalized = validate_readonly_sql(sql)
        return self._execute_query(normalized.replace("%", "%%"), (), display_sql=normalized)

    def read_financial_accounts(self, context, account_ids: list[str], account_names: list[str]) -> SQLResult:
        """고정된 매개변수 쿼리로 연간 공시 계정만 조회한다. 임의 테이블 접근은 허용하지 않는다."""
        query = """
            SELECT rcept_no, fs_div, record_index, corp_code, fiscal_year, reprt_code,
                   sj_div, account_id, account_nm, account_detail, currency,
                   thstrm_amount, frmtrm_amount
            FROM fin.dart_financial_accounts
            WHERE corp_code = %s AND fiscal_year = %s AND fs_div = %s
              AND rcept_no = %s AND reprt_code = '11011'
              AND (account_id = ANY(%s) OR
                     lower(regexp_replace(account_nm, %s, '', 'g')) = ANY(%s))
            ORDER BY record_index
        """
        parameters = (context.corp_code, context.year, "CFS" if context.scope == "consolidated" else "OFS",
                      context.rcept_no, account_ids, r"[\s()（）\[\]·ㆍ,]", account_names)
        return self._execute_query(query, parameters)

    def _execute_query(self, query: str, parameters: tuple, *, display_sql: str | None = None) -> SQLResult:
        """검사한 SQL 또는 고정 쿼리에 동일한 읽기 전용·시간·행 수 제한을 적용한다."""
        connection = None
        try:
            connection = psycopg2.connect(
                self._database_url, connect_timeout=5,
                application_name="fin_research_readonly",
                options=(f"-c search_path=pg_catalog -c statement_timeout={self.timeout_ms} "
                         "-c lock_timeout=2000 -c default_transaction_read_only=on"),
            )
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                # 바깥 쿼리에도 상한을 적용해 모델이 LIMIT을 생략해도 대량 조회하지 않는다.
                cursor.execute(
                    f"SELECT * FROM ({query}) AS _fin_result LIMIT %s",
                    (*parameters, self.max_rows + 1),
                )
                columns = [column[0] for column in cursor.description]
                if len(columns) != len(set(columns)):
                    raise SQLExecutionError("출력 열 이름이 중복됩니다. AS로 별칭을 지정하세요.")
                fetched = cursor.fetchmany(self.max_rows + 1)
                rows = [dict(zip(columns, row)) for row in fetched[:self.max_rows]]
                return SQLResult(display_sql or query, columns, rows, len(rows), len(fetched) > self.max_rows)
        except psycopg2.Error as exc:
            # DB 원본 오류에는 접속·인증 정보가 포함될 수 있어 출력하지 않는다.
            code = exc.pgcode or "connection"
            raise SQLExecutionError(
                f"PostgreSQL 조회에 실패했습니다(code={code}). 연결·권한·스키마를 확인하세요."
            ) from None
        finally:
            if connection is not None:
                with suppress(psycopg2.Error):
                    connection.rollback()
                with suppress(psycopg2.Error):
                    connection.close()
