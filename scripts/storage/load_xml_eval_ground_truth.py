"""검증된 XML 평가 정답 v1·v2 → Supabase 신규 테이블 적재.

기본: DRY-RUN (DB 연결/변경 없음).
--apply: 지정 공시의 기존 행만 지우고 v1·v2 최종본으로 교체 (단일 트랜잭션).
전제: fin.xml_eval_ground_truth_v2 테이블이 사전에 생성되어 있어야 함.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
TABLE = "fin.xml_eval_ground_truth_v2"
COLUMNS = (
    "corp_code", "fiscal_year", "statement_scope", "rcept_no",
    "question_id", "dataset_version", "question", "answer_type", "expected_value",
    "unit", "category", "sj_div", "account_id", "account_nm_raw", "account_nm_norm",
    "gold_sources", "candidate_sources", "annotation_status", "annotation_method", "source_row",
)


def clean_amount(value: object, where: str) -> tuple[str, bool]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: 금액은 빈 문자열이 아니어야 합니다.")
    cleaned = value.strip()
    number = cleaned.replace(",", "")
    if number.startswith("(") and number.endswith(")"):
        number = "-" + number[1:-1]
    try:
        parsed = Decimal(number)
    except InvalidOperation:
        raise ValueError(f"{where}: 숫자 표기가 올바르지 않습니다: {value!r}") from None
    if not parsed.is_finite():
        raise ValueError(f"{where}: 유한한 숫자가 아닙니다: {value!r}")
    return cleaned, cleaned != value


def validate_answer(item: dict, where: str) -> tuple[object, int]:
    answer_type = item.get("answer_type")
    expected = item.get("expected_value")
    if answer_type == "numeric":
        value, changed = clean_amount(expected, where)
        return value, int(changed)
    if answer_type == "comparison":
        if not isinstance(expected, dict) or len(expected) < 2:
            raise ValueError(f"{where}: comparison은 연도별 금액 2개 이상이 필요합니다.")
        cleaned = {}
        trim_count = 0
        for year, amount in expected.items():
            if not isinstance(year, str) or not re.fullmatch(r"\d{4}", year):
                raise ValueError(f"{where}: 비교 연도 형식 오류: {year!r}")
            cleaned[year], changed = clean_amount(amount, f"{where}/{year}")
            trim_count += int(changed)
        return cleaned, trim_count
    raise ValueError(f"{where}: 지원하지 않는 answer_type: {answer_type!r}")


def load_final_questions(paths: list[tuple[str, Path]]):
    items = []
    seen_ids, seen_questions = set(), set()
    statuses = Counter()
    trimmed_values = 0
    skipped = []
    for version, path in paths:
        source = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(source, list):
            raise ValueError(f"{path.name}: 최상위 JSON은 배열이어야 합니다.")
        for index, row in enumerate(source, 1):
            where = f"{path.name}:{index}"
            if not isinstance(row, dict):
                raise ValueError(f"{where}: JSON 문항은 객체여야 합니다.")
            status = row.get("annotation_status")
            statuses[(version, str(status))] += 1
            if status != "verified":
                skipped.append(f"{where} → {status}")
                continue

            qid, question = row.get("id"), row.get("question")
            if not isinstance(qid, str) or not qid.strip() or qid in seen_ids:
                raise ValueError(f"{where}: 문항 ID 누락 또는 중복: {qid!r}")
            if not isinstance(question, str) or not question.strip():
                raise ValueError(f"{where}: question 누락")
            qkey = re.sub(r"\s+", "", question)
            if qkey in seen_questions:
                raise ValueError(f"{where}: 중복 질문: {question}")
            seen_ids.add(qid)
            seen_questions.add(qkey)

            expected, count = validate_answer(row, where)
            trimmed_values += count
            unit = row.get("unit")
            if not isinstance(unit, str) or not unit.strip():
                raise ValueError(f"{where}: unit 누락")
            gold = row.get("gold_sources")
            if not isinstance(gold, list) or not gold or not all(
                isinstance(s, dict) and isinstance(s.get("source_file"), str)
                and s["source_file"].strip() and isinstance(s.get("source_xpath"), str)
                and s["source_xpath"].strip() for s in gold
            ):
                raise ValueError(f"{where}: gold_sources의 source_file/source_xpath 오류")
            candidates = row.get("candidate_sources", [])
            if not isinstance(candidates, list):
                raise ValueError(f"{where}: candidate_sources는 배열이어야 합니다.")
            account = row.get("opendart_account") or {}
            if not isinstance(account, dict):
                raise ValueError(f"{where}: opendart_account는 객체여야 합니다.")
            name = account.get("account_nm")
            if name is not None and not isinstance(name, str):
                raise ValueError(f"{where}: account_nm은 문자열이어야 합니다.")

            items.append({
                "question_id": qid, "dataset_version": version, "question": question,
                "answer_type": row["answer_type"], "expected_value": expected,
                "unit": unit.strip(), "category": row.get("category"),
                "sj_div": account.get("sj_div"), "account_id": account.get("account_id"),
                "account_nm_raw": name,
                "account_nm_norm": re.sub(r"\s+", "", name) if name is not None else None,
                "gold_sources": gold, "candidate_sources": candidates,
                "annotation_status": "verified", "annotation_method": row.get("annotation_method"),
                "source_row": row,  # 수정 전 JSON을 온전히 보존
            })
    if not items:
        raise ValueError("verified 문항이 0개라 적재할 수 없습니다.")
    return items, statuses, trimmed_values, skipped


def apply_to_supabase(args, items):
    try:
        import psycopg2
        from psycopg2.extras import Json, execute_values
        from dotenv import load_dotenv
    except ImportError:
        raise SystemExit("필수 패키지 없음: pip install psycopg2-binary python-dotenv") from None

    load_dotenv(ROOT / ".env")
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(".env의 DATABASE_URL이 없습니다.")
    host = (urlsplit(url).hostname or "").lower()
    if not (host == "supabase.co" or host.endswith(".supabase.co") or host.endswith(".pooler.supabase.com")):
        raise SystemExit("Supabase 연결 호스트가 아닙니다. DATABASE_URL을 확인하세요.")

    data = []
    for item in items:
        x = dict(item)
        x.update(corp_code=args.corp_code, fiscal_year=args.year,
                    statement_scope=args.scope, rcept_no=args.rcept_no)
        for col in ("expected_value", "gold_sources", "candidate_sources", "source_row"):
            x[col] = Json(x[col])
        data.append(tuple(x[c] for c in COLUMNS))

    key = (args.corp_code, args.year, args.scope, args.rcept_no)
    where = "corp_code=%s AND fiscal_year=%s AND statement_scope=%s AND rcept_no=%s"
    sql = f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES %s"
    print(f"Supabase 호스트: {host}")
    conn = psycopg2.connect(url, connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
                cursor.execute("SET LOCAL statement_timeout = '60s'")
                cursor.execute("SELECT to_regclass(%s)", (TABLE,))
                if cursor.fetchone()[0] is None:
                    raise RuntimeError(f"테이블이 없습니다: {TABLE} (먼저 SQL 생성 필요)")
                cursor.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE {where}", key)
                previous = cursor.fetchone()[0]
                cursor.execute(f"DELETE FROM {TABLE} WHERE {where}", key)
                execute_values(cursor, sql, data, page_size=200)
                cursor.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE {where}", key)
                final = cursor.fetchone()[0]
                if final != len(items):
                    raise RuntimeError(f"적재 건수 불일치: 파일 {len(items)} / DB {final}")
        print(f"COMMIT 완료: 동일 공시 {previous}건 → {final}건")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="XML 평가 정답(v1·v2) → 신규 Supabase 테이블")
    parser.add_argument("--corp-code", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--scope", choices=("consolidated", "separate"), required=True)
    parser.add_argument("--rcept-no", required=True)
    parser.add_argument("--apply", action="store_true", help="실제 DB 적재 (기본은 DRY-RUN)")
    args = parser.parse_args()

    if not re.fullmatch(r"\d{8}", args.corp_code):
        parser.error("--corp-code는 8자리 숫자여야 합니다.")
    if not re.fullmatch(r"\d{14}", args.rcept_no):
        parser.error("--rcept-no는 14자리 숫자여야 합니다.")
    if args.year <= 0:
        parser.error("--year는 양의 정수여야 합니다.")

    folder = ROOT / "evaluation/dataset/xml" / args.corp_code / str(args.year) / args.scope
    v1 = folder / f"{args.rcept_no}_retrieval.json"
    v2 = folder / f"{args.rcept_no}_retrieval_v2.json"
    if not v1.is_file():
        raise SystemExit(f"v1 정답 파일이 없습니다: {v1}")
    paths = [("v1", v1)] + ([("v2", v2)] if v2.is_file() else [])
    items, counts, trimmed, skipped = load_final_questions(paths)
    print(f"대상: {args.corp_code} / {args.year} / {args.scope} / {args.rcept_no}")
    print("파일:", ", ".join(f"{v}={p.name}" for v, p in paths))
    print("검증 상태:", ", ".join(f"{v}/{s}={n}" for (v, s), n in sorted(counts.items())))
    print(f"적재 대상: {len(items)}개 (v1 {sum(i['dataset_version']=='v1' for i in items)} / "
            f"v2 {sum(i['dataset_version']=='v2' for i in items)})")
    if trimmed:
        print(f"금액 문자열 양끝 공백 정리: {trimmed}개 (source_row는 원본 유지)")
    if skipped:
        print(f"verified가 아니어서 제외한 문항: {len(skipped)}개")
        for entry in skipped[:10]:
            print(" -", entry)
    if not args.apply:
        print("DRY-RUN 완료: DB는 변경하지 않았습니다.")
        return
    unfinished = {(v, s): n for (v, s), n in counts.items()
                    if s not in ("verified", "excluded", "rejected")}
    if unfinished:
        raise SystemExit(f"미검증 문항이 남아 있어 --apply 중단: {unfinished}")
    apply_to_supabase(args, items)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"검증 실패: {error}") from None
