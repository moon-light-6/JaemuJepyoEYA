"""SQL 구조 차단, 읽기 전용 실행 및 실제 LangChain 프롬프트 경로를 검증한다."""

import json
import re
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import psycopg2
from langchain_core.runnables import RunnableLambda
from sqlglot import parse_one

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fin_research.storage.sql_store import (
    VIEW_COLUMNS, PostgresReadOnlyStore, SQLExecutionError, SQLResult,
    UnsafeSQLError, format_krw, json_value, validate_readonly_sql,
)
from fin_research.tools.sql_tool import FinancialSQLTool, SQLPlan, generate_sql


VALID_SQL = (
    "SELECT company_name, amount, pdf_page, chunk_id FROM fin.v_fin_value "
    "WHERE corp_code='01160363' AND fs_scope='consolidated' "
    "AND period_end=DATE '2025-12-31' AND std_account_code='REVENUE'"
)


class SQLGuardTests(unittest.TestCase):
    def test_accepts_source_preserving_select_and_aggregation(self):
        queries = [VALID_SQL,
            "SELECT ROUND(AVG(amount)::numeric, 2) FROM fin.v_fin_value",
            "SELECT company_name FROM fin.v_fin_value WHERE company_name='DROP TABLE users;'",
            "SELECT x.company_name FROM (SELECT company_name FROM fin.v_fin_value) AS x",
            "SELECT a.company_name FROM fin.v_fin_value a JOIN fin.v_fin_std_value b "
            "ON a.rcept_no=b.rcept_no WHERE a.amount IS NOT NULL",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                self.assertTrue(validate_readonly_sql(sql).startswith("SELECT"))

    def test_rejects_writes_system_access_and_side_effects(self):
        queries = [
            "DROP TABLE fin.company",
            "SELECT * FROM fin.v_fin_value; DELETE FROM fin.company",
            "SELECT * INTO copied FROM fin.v_fin_value",
            "SELECT * FROM fin.v_fin_value FOR UPDATE",
            "WITH x AS (DELETE FROM fin.company RETURNING *) SELECT * FROM x",
            "WITH RECURSIVE x AS (SELECT * FROM fin.v_fin_value) SELECT * FROM x",
            "SELECT * FROM public.users",
            "SELECT * FROM pg_catalog.pg_authid",
            "SELECT * FROM fin.company",
            "SELECT * FROM v_fin_value",
            "SELECT pg_sleep(10) FROM fin.v_fin_value",
            "SELECT set_config('search_path','public',true) FROM fin.v_fin_value",
            "SELECT public.evil(amount) FROM fin.v_fin_value",
            "SELECT pg_catalog.abs(amount) FROM fin.v_fin_value",
            "SELECT nextval('seq') FROM fin.v_fin_value",
            "SELECT amount::regclass FROM fin.v_fin_value",
            "SELECT amount::public.evil_type FROM fin.v_fin_value",
            "SELECT * FROM generate_series(1,100)",
            "SELECT 1",
            "SELECT * FROM fin.v_fin_value UNION SELECT * FROM public.users",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                with self.assertRaises(UnsafeSQLError):
                    validate_readonly_sql(sql)

    def test_malformed_or_oversized_input(self):
        for sql in (None, "", "SELECT (", "x" * 20001):
            with self.subTest(sql=str(sql)[:30]):
                with self.assertRaises(UnsafeSQLError):
                    validate_readonly_sql(sql)

    def test_prompt_view_contract_matches_actual_schema(self):
        schema = (ROOT / "src/fin_research/storage/schema.sql").read_text(encoding="utf-8")
        for name, expected in VIEW_COLUMNS.items():
            with self.subTest(view=name):
                query = re.search(rf"CREATE OR REPLACE VIEW {name} AS\s*(.*?);", schema, re.S).group(1)
                self.assertEqual(parse_one(query, read="postgres").named_selects, expected)


class SQLStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value = Mock()
        self.cursor.description = [("amount",)]
        self.cursor.fetchmany.return_value = [(Decimal("123456789012345678.25"),), (None,), (5,)]
        self.connect = patch("fin_research.storage.sql_store.psycopg2.connect", return_value=self.connection)
        self.mock_connect = self.connect.start()
        self.addCleanup(self.connect.stop)

    def test_readonly_limits_precision_and_cleanup(self):
        store = PostgresReadOnlyStore("postgresql://unused", max_rows=2)
        result = store.execute("SELECT amount FROM fin.v_fin_value")
        self.connection.set_session.assert_called_once_with(readonly=True, autocommit=False)
        options = self.mock_connect.call_args.kwargs["options"]
        self.assertIn("statement_timeout=10000", options)
        self.assertIn("default_transaction_read_only=on", options)
        self.assertEqual(self.cursor.execute.call_args.args[1], (3,))
        self.assertEqual(result.row_count, 2)
        self.assertTrue(result.truncated)
        self.assertIsNone(result.rows[1]["amount"])
        encoded = json.dumps(result.to_dict(), default=json_value)
        self.assertIn('"123456789012345678.25"', encoded)
        self.connection.rollback.assert_called_once()
        self.connection.close.assert_called_once()

    def test_empty_result_is_not_zero(self):
        self.cursor.fetchmany.return_value = []
        result = PostgresReadOnlyStore("unused").execute(VALID_SQL)
        self.assertEqual(result.rows, [])
        self.assertFalse(result.truncated)

    def test_blocked_sql_never_opens_connection(self):
        with self.assertRaises(UnsafeSQLError):
            PostgresReadOnlyStore("unused").execute("DELETE FROM fin.company")
        self.mock_connect.assert_not_called()

    def test_like_percent_is_escaped_for_driver_parameters(self):
        """LIKE의 %를 psycopg2 매개변수 문법으로 잘못 해석하지 않게 한다."""
        result = PostgresReadOnlyStore("unused").execute(
            "SELECT amount FROM fin.v_fin_value WHERE account_name_raw LIKE '%영업이익%'"
        )
        self.assertIn("%%영업이익%%", self.cursor.execute.call_args.args[0])
        self.assertIn("%영업이익%", result.sql)

    def test_connection_error_does_not_expose_credentials(self):
        self.mock_connect.side_effect = psycopg2.OperationalError("secret_password@private-host")
        with self.assertRaises(SQLExecutionError) as caught:
            PostgresReadOnlyStore("unused").execute(VALID_SQL)
        self.assertNotIn("secret_password", str(caught.exception))
        self.assertNotIn("private-host", str(caught.exception))

    def test_query_timeout_closes_connection(self):
        self.cursor.execute.side_effect = psycopg2.errors.QueryCanceled("sensitive database detail")
        with self.assertRaises(SQLExecutionError):
            PostgresReadOnlyStore("unused").execute(VALID_SQL)
        self.connection.rollback.assert_called_once()
        self.connection.close.assert_called_once()

    def test_duplicate_output_column_names_are_not_silently_lost(self):
        self.cursor.description = [("amount",), ("amount",)]
        with self.assertRaises(SQLExecutionError):
            PostgresReadOnlyStore("unused").execute(VALID_SQL)

    def test_resource_bounds_and_date_serialization(self):
        for kwargs in ({"max_rows": 0}, {"max_rows": True}, {"timeout_ms": 0}):
            with self.assertRaises(ValueError):
                PostgresReadOnlyStore("unused", **kwargs)
        self.assertEqual(json_value(date(2025, 12, 31)), "2025-12-31")

    def test_amount_display_preserves_original_value(self):
        original = SQLResult(VALID_SQL, ["amount"], [{"amount": 2531576384113}], 1, False)
        result = original.to_dict()
        self.assertEqual(result["rows"][0]["amount"], 2531576384113)
        self.assertEqual(result["rows"][0]["amount_display"], "약 2.532조 원")
        self.assertEqual(original.rows, [{"amount": 2531576384113}])
        self.assertEqual(original.columns, ["amount"])
        self.assertEqual(format_krw(-2531576384113), "약 -2.532조 원")
        self.assertEqual(format_krw(123456789), "약 1.235억 원")
        self.assertEqual(format_krw(12345), "약 1.235만 원")
        self.assertEqual(format_krw(0), "0원")
        self.assertIsNone(format_krw(None))


class SQLGenerationTests(unittest.TestCase):
    def model(self, response):
        model = Mock()
        self.prompts = []
        def reply(prompt):
            self.prompts.append(prompt.to_string())
            return response
        model.with_structured_output.return_value = RunnableLambda(reply)
        return model

    def test_schema_prompt_generation_and_execution(self):
        model = self.model({"status": "ready", "sql": VALID_SQL, "reason": "연결 2025 실적 조회"})
        store = Mock()
        store.execute.return_value = SQLResult(VALID_SQL, ["amount"], [{"amount": 100}], 1, False)
        result = FinancialSQLTool(model, store).run("에코프로비엠의 2025년 연결 매출")
        self.assertEqual(result["result"]["rows"], [{"amount": 100, "amount_display": "100원"}])
        self.assertIn("fin.v_fin_value", self.prompts[0])
        self.assertIn("fiscal_year는 보고서의 사업연도", self.prompts[0])
        self.assertIn("에코프로비엠", self.prompts[0])
        model.with_structured_output.assert_called_once_with(SQLPlan, method="json_schema", strict=True)

    def test_clarification_does_not_query_database(self):
        model = self.model({"status": "needs_clarification", "sql": None, "reason": "연결/별도 확인 필요"})
        store = Mock()
        result = FinancialSQLTool(model, store).run("매출 알려줘")
        self.assertIsNone(result["result"])
        store.execute.assert_not_called()

    def test_unsafe_model_output_is_blocked_before_execution(self):
        model = self.model({"status": "ready", "sql": "SELECT pg_sleep(10) FROM fin.v_fin_value", "reason": "테스트"})
        store = Mock()
        with self.assertRaises(UnsafeSQLError):
            FinancialSQLTool(model, store).run("질문")
        store.execute.assert_not_called()

    def test_invalid_structured_output(self):
        for response in (
            {"status": "ready", "sql": None, "reason": "누락"},
            {"status": "needs_clarification", "sql": VALID_SQL, "reason": "모순"},
        ):
            with self.assertRaises(ValueError):
                generate_sql("질문", self.model(response))

    def test_report_year_only_query_is_repaired_before_execution(self):
        bad_sql = "SELECT period_end, amount FROM fin.v_fin_value WHERE fiscal_year=2025"
        responses = iter([
            {"status": "ready", "sql": bad_sql, "reason": "보고서 연도만 선택"},
            {"status": "ready", "sql": VALID_SQL, "reason": "실적 기간 수정"},
        ])
        model = Mock()
        prompts = []
        def reply(prompt):
            prompts.append(prompt.to_string())
            return next(responses)
        model.with_structured_output.return_value = RunnableLambda(reply)
        progress = []
        plan = generate_sql("2025 실적", model, progress=progress.append)
        self.assertEqual(len(prompts), 2)
        self.assertIn("period_end 또는 period_role", prompts[1])
        self.assertIn("2025-12-31", plan.sql)
        self.assertIn("SQL 생성 중", progress[0])
        self.assertTrue(any("SQL 검사 실패" in message for message in progress))
        self.assertTrue(any("시도 2/2" in message for message in progress))
        self.assertTrue(any("SQL 검사 통과" in message for message in progress))

    def test_unrepaired_period_query_never_executes(self):
        model = self.model({"status": "ready", "sql":
            "SELECT period_end, amount FROM fin.v_fin_value WHERE fiscal_year=2025", "reason": "미수정"})
        store = Mock()
        with self.assertRaises(UnsafeSQLError):
            FinancialSQLTool(model, store).run("2025년 실적")
        store.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
