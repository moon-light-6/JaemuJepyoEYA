"""연결재무제표 수치를 PostgreSQL(fin 스키마)에 적재한다.

입력:  data/processed/dart/{corp_code}/{year}/{rcept_no}_pages.json, _chunks.json
출력:  fin.company / report / report_period / fin_statement / fin_line_item / fin_value

사용 예:
  # DB 없이 파싱 결과와 합계 검증만 확인
  python scripts/storage/load_financials.py --corp-code 01160363 --dry-run

  # 적재 (접속 정보는 .env 의 DATABASE_URL 또는 --database-url)
  python scripts/storage/load_financials.py --corp-code 01160363

스키마는 먼저 만들어 두어야 한다: psql -f src/fin_research/storage/schema.sql
같은 보고서를 다시 적재하면 그 보고서의 표를 지우고 다시 넣는다(중복 없음).
주석 표(note_*)는 이 스크립트 범위가 아니다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.statement_parser import (  # noqa: E402
    ROLES,
    ParseResult,
    parse_statements,
    validate,
)

COMPANY_RE = re.compile(
    r'#\s*종목코드:\s*(\S+)\s*\n\s*-\s*name:\s*"([^"]+)"\s*\n\s*corp_code:\s*"(\d{8})"'
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="연결재무제표 수치를 PostgreSQL에 적재합니다.")
    parser.add_argument("--corp-code", required=True, help="DART 고유번호 8자리 (예: 01160363)")
    parser.add_argument("--year", default="2025", help="사업연도 폴더 (기본 2025)")
    parser.add_argument("--rcept-no", help="접수번호. 폴더에 보고서가 둘 이상일 때만 지정")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data" / "processed" / "dart")
    parser.add_argument("--database-url", help="미지정 시 환경변수/.env 의 DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 파싱·검증 결과만 출력")
    parser.add_argument("--force", action="store_true", help="합계 검증이 실패해도 적재 (권장하지 않음)")
    return parser.parse_args()


def find_input_files(args: argparse.Namespace) -> tuple[str, Path, Path]:
    folder = args.data_dir / args.corp_code / args.year
    candidates = sorted(
        m.group(1)
        for path in folder.glob("*_pages.json")
        if (m := re.fullmatch(r"(\d{14})_pages\.json", path.name))
    )
    if args.rcept_no:
        candidates = [c for c in candidates if c == args.rcept_no]
    if len(candidates) != 1:
        raise SystemExit(f"{folder} 에서 보고서 1건을 특정하지 못했습니다: {candidates} (--rcept-no 로 지정)")
    rcept_no = candidates[0]
    return rcept_no, folder / f"{rcept_no}_pages.json", folder / f"{rcept_no}_chunks.json"


def company_info(corp_code: str, fallback_name: str) -> tuple[str, str | None]:
    config = PROJECT_ROOT / "configs" / "companies.yaml"
    if config.is_file():
        for stock_code, name, code in COMPANY_RE.findall(config.read_text(encoding="utf-8")):
            if code == corp_code:
                return name, stock_code if re.fullmatch(r"[0-9A-Z]{6}", stock_code) else None
    return fallback_name, None


def merged_periods(result: ParseResult) -> list[dict]:
    """표마다 따로 읽은 기간(제N기/시작일/종료일)을 합친다. 재무상태표엔 시작일이 없다."""
    merged: dict[int, dict] = {}
    for statement in result.statements:
        for idx, period in enumerate(statement.periods):
            slot = merged.setdefault(idx, {"term_no": period["term_no"], "start": None, "end": period["end"]})
            if slot["term_no"] != period["term_no"] or slot["end"] != period["end"]:
                raise SystemExit(
                    f"표마다 기간이 다릅니다: {statement.statement_type} {idx}번째 열 {period} vs {slot}"
                )
            slot["start"] = slot["start"] or period["start"]
    return [merged[i] for i in sorted(merged)]


def print_summary(result: ParseResult, checks: list) -> bool:
    for s in result.statements:
        values = sum(len(line.values) for line in s.lines)
        print(f"  {s.statement_type:<3} 행 {len(s.lines):>3}  값 {values:>4}  단위 x{s.unit_multiplier}  쪽 {s.pdf_page_start}-{s.pdf_page_end}")
    print("합계 검증:")
    failed = False
    for name, ok, detail in checks:
        mark = {True: "PASS", False: "FAIL", None: "N/A "}[ok]
        failed = failed or ok is False
        print(f"  {mark} {name} ({detail})")
    for warning in result.warnings:
        print(f"  경고: {warning}")
    return not failed


KEY_STD_CODES = (
    "ASSETS_TOTAL", "LIABILITIES_TOTAL", "EQUITY_TOTAL",
    "REVENUE", "OPERATING_INCOME", "NET_INCOME",
    "CASH_FLOW_OPERATING", "CASH_END",
)


def load_into_db(database_url: str, report: dict, result: ParseResult, chunks: list[dict]) -> None:
    try:
        import psycopg2
        from psycopg2.extras import execute_values
    except ImportError:
        raise SystemExit("psycopg2 가 없습니다: pip install psycopg2-binary")

    periods = merged_periods(result)
    connection = psycopg2.connect(database_url)
    try:
        with connection, connection.cursor() as cur:  # with connection: 성공 시 commit, 예외 시 rollback
            cur.execute(
                """INSERT INTO fin.company (corp_code, stock_code, name) VALUES (%s, %s, %s)
                   ON CONFLICT (corp_code) DO UPDATE
                   SET name = EXCLUDED.name, stock_code = COALESCE(EXCLUDED.stock_code, fin.company.stock_code)""",
                (report["corp_code"], report["stock_code"], report["name"]),
            )
            cur.execute(
                """INSERT INTO fin.report (rcept_no, corp_code, fiscal_year, source_file) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (rcept_no) DO UPDATE
                   SET fiscal_year = EXCLUDED.fiscal_year, source_file = EXCLUDED.source_file
                   RETURNING report_id""",
                (report["rcept_no"], report["corp_code"], report["fiscal_year"], report["source_file"]),
            )
            report_id = cur.fetchone()[0]

            # 출처: 청크 원문을 먼저 넣는다 (값/행의 chunk_id 가 이를 가리킨다)
            execute_values(
                cur,
                """INSERT INTO fin.source_chunk (chunk_id, report_id, pdf_page, page_id, section, text)
                   VALUES %s
                   ON CONFLICT (chunk_id) DO UPDATE
                   SET report_id = EXCLUDED.report_id, pdf_page = EXCLUDED.pdf_page, page_id = EXCLUDED.page_id,
                       section = EXCLUDED.section, text = EXCLUDED.text""",
                [(c["chunk_id"], report_id, c["pdf_page"], c.get("page_id"), c.get("section"), c["text"])
                 for c in chunks],
                page_size=500,
            )
            print(f"  적재 source_chunk: {len(chunks)}개")

            period_ids: list[int] = []
            for idx, period in enumerate(periods):
                cur.execute(
                    """INSERT INTO fin.report_period (report_id, role, term_no, period_start, period_end)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (report_id, role) DO UPDATE
                       SET term_no = EXCLUDED.term_no, period_end = EXCLUDED.period_end,
                           period_start = COALESCE(EXCLUDED.period_start, fin.report_period.period_start)
                       RETURNING period_id""",
                    (report_id, ROLES[idx], period["term_no"], period["start"], period["end"]),
                )
                period_ids.append(cur.fetchone()[0])

            for s in result.statements:
                cur.execute(
                    """INSERT INTO fin.fin_statement
                         (report_id, fs_scope, statement_type, title_raw, unit_label_raw, unit_multiplier,
                          pdf_page_start, pdf_page_end)
                       VALUES (%s, 'consolidated', %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (report_id, fs_scope, statement_type) DO UPDATE
                       SET title_raw = EXCLUDED.title_raw, unit_label_raw = EXCLUDED.unit_label_raw,
                           unit_multiplier = EXCLUDED.unit_multiplier,
                           pdf_page_start = EXCLUDED.pdf_page_start, pdf_page_end = EXCLUDED.pdf_page_end
                       RETURNING statement_id""",
                    (report_id, s.statement_type, s.title_raw, s.unit_label_raw, s.unit_multiplier,
                     s.pdf_page_start, s.pdf_page_end),
                )
                statement_id = cur.fetchone()[0]
                cur.execute("DELETE FROM fin.fin_line_item WHERE statement_id = %s", (statement_id,))  # 값은 cascade

                line_ids = execute_values(
                    cur,
                    """INSERT INTO fin.fin_line_item
                         (statement_id, line_no, account_name_raw, account_name_norm, indent_level,
                          is_total, note_refs, pdf_page, chunk_id)
                       VALUES %s RETURNING line_id""",
                    [(statement_id, l.line_no, l.label_raw, l.label_norm, l.indent, l.is_total,
                      l.note_refs, l.pdf_page, l.chunk_id) for l in s.lines],
                    page_size=1000,
                    fetch=True,
                )
                rows = [
                    (line_id[0], period_ids[v.period_idx], v.column_key, v.raw, v.reported, v.amount,
                     v.status, v.pdf_page, v.chunk_id)
                    for line_id, line in zip(line_ids, s.lines)
                    for v in line.values
                ]
                execute_values(
                    cur,
                    """INSERT INTO fin.fin_value
                         (line_id, period_id, column_key, amount_raw, amount_as_reported, amount,
                          value_status, pdf_page, chunk_id)
                       VALUES %s""",
                    rows,
                    page_size=1000,
                )
                # 표준 계정코드 매핑: 같은 코드는 표 안에서 첫 번째 행에만 붙인다 (유니크 인덱스 보호)
                cur.execute(
                    """UPDATE fin.fin_line_item AS l SET std_account_code = m.std_account_code
                       FROM (SELECT DISTINCT ON (a.std_account_code) li.line_id, a.std_account_code
                             FROM fin.fin_line_item li
                             JOIN fin.std_account_alias a
                               ON a.statement_type = %s AND a.alias_norm = li.account_name_norm
                             WHERE li.statement_id = %s
                             ORDER BY a.std_account_code, li.line_no) AS m
                       WHERE l.line_id = m.line_id""",
                    (s.statement_type, statement_id),
                )
                print(f"  적재 {s.statement_type}: 행 {len(line_ids)}, 값 {len(rows)}, 표준계정 매핑 {cur.rowcount}행")

            cur.execute(
                """SELECT DISTINCT l.std_account_code FROM fin.fin_line_item l
                   JOIN fin.fin_statement s USING (statement_id)
                   WHERE s.report_id = %s AND l.std_account_code IS NOT NULL""",
                (report_id,),
            )
            mapped = {row[0] for row in cur.fetchall()}
            missing = [code for code in KEY_STD_CODES if code not in mapped]
            if missing:
                print(f"  경고: 핵심 표준 계정이 매핑되지 않음 {missing} (비율 뷰 일부가 NULL). std_account_alias 에 별칭 추가 필요")
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    rcept_no, pages_path, chunks_path = find_input_files(args)
    pages = json.loads(pages_path.read_text(encoding="utf-8"))
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    name, stock_code = company_info(args.corp_code, pages[0].get("company", ""))
    print(f"{name} ({args.corp_code}, 종목 {stock_code}) rcept_no={rcept_no}")

    result = parse_statements(pages, chunks)
    checks = validate(result)
    all_passed = print_summary(result, checks)
    if not result.statements:
        print("파싱된 표가 없어 종료합니다.")
        return 1
    if args.dry_run:
        print("(dry-run: DB에 쓰지 않았습니다)")
        return 0 if all_passed else 1
    if not all_passed and not args.force:
        print("합계 검증이 실패해 적재하지 않았습니다. (원인 확인 후, 그래도 넣으려면 --force)")
        return 1

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        try:
            from dotenv import load_dotenv

            load_dotenv(PROJECT_ROOT / ".env")
            database_url = os.environ.get("DATABASE_URL")
        except ImportError:
            pass
    if not database_url:
        raise SystemExit("DATABASE_URL 이 없습니다. .env 에 넣거나 --database-url 로 지정하세요.")

    report = {
        "corp_code": args.corp_code,
        "stock_code": stock_code,
        "name": name,
        "rcept_no": rcept_no,
        "fiscal_year": int(pages[0]["fiscal_year"]),
        "source_file": pages[0].get("source_file"),
    }
    load_into_db(database_url, report, result, chunks)
    print("완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
