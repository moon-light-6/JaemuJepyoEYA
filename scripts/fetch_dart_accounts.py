"""Fetch DART annual-report financial statement accounts into a loader-ready JSON.

Use from repository root (requires src/fin_research/ingestion/auto_resolve.py):
    python scripts/fetch_dart_accounts.py --corp-code 01095722 --year 2025 --scope consolidated
    python scripts/fetch_dart_accounts.py --corp-code 01095722 --year 2025 --scope separate

The output is consumed by scripts/load_dart_accounts.py. No database writes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'src'))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='OpenDART 재무계정 API → Supabase 적재용 JSON')
    parser.add_argument('--corp-code', required=True, help='8자리 DART 기업고유번호')
    parser.add_argument('--year', type=int, required=True, help='사업연도, 예: 2025')
    parser.add_argument('--scope', choices=('consolidated', 'separate'), required=True)
    parser.add_argument('--expected-rcept-no', help='기존 XML 공시와 일치해야 할 14자리 접수번호')
    parser.add_argument('--overwrite', action='store_true', help='기존 출력 JSON 덮어쓰기 허용')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not re.fullmatch(r'\d{8}', args.corp_code):
        raise SystemExit('--corp-code는 8자리 DART 기업고유번호여야 합니다.')
    if args.year < 2000 or args.year > 2100:
        raise SystemExit('--year 값을 확인하세요.')
    if args.expected_rcept_no and not re.fullmatch(r'\d{14}', args.expected_rcept_no):
        raise SystemExit('--expected-rcept-no는 14자리 접수번호여야 합니다.')

    out = PROJECT_ROOT / 'data' / 'processed' / 'financial' / args.corp_code / str(args.year) / f'{args.year}_{args.scope}_accounts.json'
    if out.exists() and not args.overwrite:
        raise SystemExit(f'기존 파일이 있습니다. 덮어쓰지 않았습니다: {out}\n재조회하려면 --overwrite를 추가하세요.')

    try:
        from fin_research.ingestion.dart_client import DartClient
        from fin_research.ingestion.auto_resolve import fetch_financial_accounts
    except ImportError as exc:
        raise SystemExit('프로젝트의 data/pdf_processing XML 파이프라인 코드가 필요합니다. 현재 브랜치를 확인하세요.') from exc

    rows = fetch_financial_accounts(DartClient.from_environment(), args.corp_code, args.year, args.scope)
    if not rows:
        raise SystemExit('API 조회 결과가 없습니다. 기업/연도/연결·별도 기준을 확인하세요.')
    if any(str(r.get('corp_code', '')) != args.corp_code or str(r.get('bsns_year', '')) != str(args.year) for r in rows):
        raise SystemExit('API 결과의 기업고유번호 또는 사업연도가 요청값과 다릅니다.')
    receipts = {str(row.get('rcept_no', '')) for row in rows}
    if len(receipts) != 1 or not all(re.fullmatch(r'\d{14}', rec) for rec in receipts):
        raise SystemExit(f'API 결과에 서로 다른 접수번호가 섞였거나 접수번호 형식이 잘못되었습니다: {sorted(receipts)}')
    receipt = next(iter(receipts))
    if args.expected_rcept_no and receipt != args.expected_rcept_no:
        raise SystemExit(f'공시번호 불일치: API={receipt}, XML={args.expected_rcept_no}. JSON을 저장하지 않았습니다.')
    expected_div = 'CFS' if args.scope == 'consolidated' else 'OFS'
    if any(str(r.get('reprt_code', '')) != '11011' for r in rows):
        raise SystemExit('API 응답이 사업보고서(11011)인지 확인할 수 없어 중단합니다.')

    payload = {
        'corp_code': args.corp_code,
        'fiscal_year': args.year,
        'fs_div': expected_div,
        'source_api': 'fnlttSinglAcntAll',
        'accounts': rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'조회/저장 완료: 기업 {args.corp_code} / {args.year} / {expected_div} / 공시 {receipt} / 계정 {len(rows)}건')
    print(f'수치용 파일: {out}')
    print('다음 단계: python scripts/load_dart_accounts.py --corp-code ... --year ... --scope ... --expected-rcept-no ... (--apply)')


if __name__ == '__main__':
    main()
