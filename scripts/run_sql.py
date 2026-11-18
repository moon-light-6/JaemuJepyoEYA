"""SQL 초안 생성, 검사 및 PostgreSQL 읽기 전용 조회를 실행한다."""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.storage.sql_store import (
    PostgresReadOnlyStore, SQLExecutionError, json_value, validate_readonly_sql,
)
from fin_research.tools.sql_tool import FinancialSQLTool, generate_sql


def main():
    parser = argparse.ArgumentParser(description="재무 PostgreSQL 조회 (기본 최대 100행)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--question", help="GPT로 SQL을 생성할 질문")
    source.add_argument("--sql", help="모델 없이 직접 검사·실행할 SELECT")
    parser.add_argument("--plan-only", action="store_true", help="DB 접속 없이 SQL 생성·검사만 실행")
    parser.add_argument("--model", default="small")
    parser.add_argument("--max-rows", type=int, default=100)
    parser.add_argument("--timeout-ms", type=int, default=10000)
    parser.add_argument("--out", type=Path, help="조회 결과 JSON 저장 경로")
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        store = None if args.plan_only else PostgresReadOnlyStore(
            max_rows=args.max_rows, timeout_ms=args.timeout_ms,
        )
        if args.sql:
            sql = validate_readonly_sql(args.sql)
            output = {"sql": sql, "executed": False} if args.plan_only else store.execute(sql).to_dict()
        else:
            from fin_research.llm.client import get_chat_model
            model = get_chat_model(args.model)
            output = (
                {"plan": generate_sql(args.question, model).model_dump(), "executed": False}
                if args.plan_only else FinancialSQLTool(model, store).run(args.question)
            )
        content = json.dumps(output, ensure_ascii=False, indent=2, default=json_value)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(content + "\n", encoding="utf-8")
        print(content)
    except (ValueError, SQLExecutionError) as exc:
        parser.exit(1, f"오류: {exc}\n")


if __name__ == "__main__":
    main()
