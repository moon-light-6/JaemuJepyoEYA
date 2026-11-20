"""Load OpenDART fnlttSinglAcntAll JSON into Supabase PostgreSQL.

This loader creates a separate fin.dart_financial_accounts table, preserving the
existing fin.fin_value and related PDF-oriented tables. It does not load Chroma
chunks, XML text, or authentication data.

Usage (from repository root, with this file placed in scripts/):
    python scripts/load_dart_accounts.py
    python scripts/load_dart_accounts.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
from psycopg2.extras import Json, execute_values
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS fin.dart_financial_accounts (
    rcept_no           varchar(14) NOT NULL,
    fs_div             varchar(3) NOT NULL CHECK (fs_div IN ('CFS', 'OFS')),
    record_index       integer NOT NULL CHECK (record_index > 0),
    corp_code          varchar(8) NOT NULL,
    fiscal_year        smallint NOT NULL,
    reprt_code         text,
    sj_div             text,
    sj_nm              text,
    account_id         text,
    account_nm         text NOT NULL,
    account_detail     text,
    thstrm_nm          text,
    thstrm_amount      numeric,
    frmtrm_nm          text,
    frmtrm_amount      numeric,
    bfefrmtrm_nm       text,
    bfefrmtrm_amount   numeric,
    ord                integer,
    currency           text,
    source_row         jsonb NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rcept_no, fs_div, record_index)
);
CREATE INDEX IF NOT EXISTS dart_fin_accounts_lookup_idx
ON fin.dart_financial_accounts (corp_code, fiscal_year, fs_div, account_nm);
"""

COLUMNS = [
    'rcept_no', 'fs_div', 'record_index', 'corp_code', 'fiscal_year',
    'reprt_code', 'sj_div', 'sj_nm', 'account_id', 'account_nm', 'account_detail',
    'thstrm_nm', 'thstrm_amount', 'frmtrm_nm', 'frmtrm_amount',
    'bfefrmtrm_nm', 'bfefrmtrm_amount', 'ord', 'currency', 'source_row',
]


def to_number(value, index, field):
    if value is None or str(value).strip() in ('', '-', '－'):
        return None
    raw = str(value).strip().replace(',', '').replace(' ', '')
    if raw.startswith('(') and raw.endswith(')'):
        raw = '-' + raw[1:-1]
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f'{index}번 계정의 {field}이 숫자가 아닙니다: {value!r}') from None
    if not amount.is_finite():
        raise ValueError(f'{index}번 계정의 {field}이 유효한 숫자가 아닙니다: {value!r}')
    return amount


def main():
    parser = argparse.ArgumentParser(description='OpenDART 재무계정 JSON → Supabase PostgreSQL')
    parser.add_argument('--corp-code', default='01095722')
    parser.add_argument('--year', type=int, default=2025)
    parser.add_argument('--scope', choices=('consolidated', 'separate'), default='consolidated')
    parser.add_argument('--expected-rcept-no', help='XML 공시와 같아야 하는 접수번호(다르면 중단)')
    parser.add_argument('--apply', action='store_true', help='지정할 때만 실제 DB에 쓰기')
    args = parser.parse_args()

    data_file = (PROJECT_ROOT / 'data' / 'processed' / 'financial' /
                 args.corp_code / str(args.year) /
                 f'{args.year}_{args.scope}_accounts.json')
    if not data_file.is_file():
        raise SystemExit(f'수치용 JSON을 찾을 수 없습니다: {data_file}')
    payload = json.loads(data_file.read_text(encoding='utf-8'))
    expected_fs_div = 'CFS' if args.scope == 'consolidated' else 'OFS'
    if (payload.get('corp_code') != args.corp_code or
        payload.get('fiscal_year') != args.year or
        payload.get('fs_div') != expected_fs_div or
        payload.get('source_api') != 'fnlttSinglAcntAll'):
        raise SystemExit('입력 JSON의 기업/연도/연결별도/API 정보가 요청값과 일치하지 않습니다.')
    rows = payload.get('accounts')
    if not isinstance(rows, list) or not rows:
        raise SystemExit('accounts 배열이 비어 있거나 올바르지 않습니다.')

    receipts = set()
    values = []
    for i, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f'{i}번 계정이 JSON 객체가 아닙니다.')
        if str(item.get('corp_code', '')) != args.corp_code or str(item.get('bsns_year', '')) != str(args.year):
            raise SystemExit(f'{i}번 계정의 기업/사업연도가 요청값과 다릅니다.')
        receipt = str(item.get('rcept_no', ''))
        if len(receipt) != 14 or not receipt.isdigit():
            raise SystemExit(f'{i}번 계정의 접수번호가 유효하지 않습니다: {receipt!r}')
        receipts.add(receipt)
        if not item.get('account_nm'):
            raise SystemExit(f'{i}번 계정의 계정명이 비어 있습니다.')
        ord_raw = item.get('ord')
        try:
            order = int(ord_raw) if ord_raw not in (None, '') else None
        except (ValueError, TypeError):
            raise SystemExit(f'{i}번 계정의 ord가 정수가 아닙니다.') from None
        values.append((
            receipt, expected_fs_div, i, args.corp_code, args.year,
            item.get('reprt_code'), item.get('sj_div'), item.get('sj_nm'),
            item.get('account_id'), item['account_nm'], item.get('account_detail'),
            item.get('thstrm_nm'), to_number(item.get('thstrm_amount'), i, 'thstrm_amount'),
            item.get('frmtrm_nm'), to_number(item.get('frmtrm_amount'), i, 'frmtrm_amount'),
            item.get('bfefrmtrm_nm'), to_number(item.get('bfefrmtrm_amount'), i, 'bfefrmtrm_amount'),
            order, item.get('currency'), Json(item),
        ))
    if len(receipts) != 1:
        raise SystemExit(f'서로 다른 접수번호가 섞여 있습니다: {sorted(receipts)}')
    receipt = next(iter(receipts))
    if args.expected_rcept_no and receipt != args.expected_rcept_no:
        raise SystemExit(
            f'수치 API 접수번호 {receipt}와 XML 접수번호 {args.expected_rcept_no}가 달라 적재를 중단합니다.'
        )
    print(f'확인 완료: 기업 {args.corp_code} / {args.year} / {expected_fs_div} / 접수번호 {receipt} / 계정 {len(values)}건')
    if not args.apply:
        print('DRY-RUN: DB를 변경하지 않았습니다. 실제 적재: --apply 옵션을 추가하세요.')
        return

    load_dotenv(PROJECT_ROOT / '.env')
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        raise SystemExit('.env의 DATABASE_URL이 없습니다.')
    host = urlsplit(database_url).hostname or ''
    if not (host == 'supabase.co' or host.endswith('.supabase.co') or host.endswith('.pooler.supabase.com')):
        raise SystemExit('DATABASE_URL 호스트가 Supabase 도메인이 아닙니다. 잘못된 DB에 적재하지 않도록 중단합니다.')
    print('적재 대상 호스트:', host)

    cols = ', '.join(COLUMNS)
    updates = ', '.join(f'{col}=EXCLUDED.{col}' for col in COLUMNS if col not in ('rcept_no', 'fs_div', 'record_index'))
    statement = f'INSERT INTO fin.dart_financial_accounts ({cols}) VALUES %s ON CONFLICT (rcept_no, fs_div, record_index) DO UPDATE SET {updates}'
    conn = psycopg2.connect(database_url, connect_timeout=8)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regnamespace('fin')")
                if cur.fetchone()[0] is None:
                    raise ValueError('Supabase에 fin 스키마가 없습니다. 기존 12개 테이블이 있는 DB인지 확인하세요.')
                cur.execute(CREATE_SQL)
                cur.execute('SELECT COUNT(*) FROM fin.dart_financial_accounts WHERE rcept_no=%s AND fs_div=%s', (receipt, expected_fs_div))
                old_count = cur.fetchone()[0]
                if old_count > len(values):
                    raise ValueError(f'기존에 동일 공시의 {old_count}건이 있습니다. 이번 {len(values)}건보다 많아 덮어쓰지 않습니다.')
                execute_values(cur, statement, values, page_size=200)
                cur.execute('SELECT COUNT(*) FROM fin.dart_financial_accounts WHERE rcept_no=%s AND fs_div=%s', (receipt, expected_fs_div))
                count = cur.fetchone()[0]
                if count != len(values):
                    raise ValueError(f'적재 수 불일치: 예상 {len(values)}건 / 실제 {count}건')
        print(f'적재 완료: fin.dart_financial_accounts / {count}건 (트랜잭션 COMMIT)')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
