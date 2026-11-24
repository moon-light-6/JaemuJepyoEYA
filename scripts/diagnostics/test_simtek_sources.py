"""Read-only smoke test: Supabase financial value + Chroma XML evidence.

From the repository root, after placing this file in scripts/:
    python scripts/diagnostics/test_simtek_sources.py

This checks one known 2025 consolidated operating-income claim for Simmtech.
It does not change either database or integrate the project's Researcher agents.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

CORP_CODE = "01095722"
FISCAL_YEAR = 2025
RECEIPT = "20260317000641"
COLLECTION = f"fin_xml_{CORP_CODE}_{FISCAL_YEAR}_consolidated_{RECEIPT}_kure_v1"


def main() -> None:
    load_dotenv(ROOT / ".env")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL이 설정되지 않았습니다.")

    # Read-only SQL. Require unambiguous company, scope, year, receipt and account.
    with psycopg2.connect(database_url, connect_timeout=8) as connection:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT account_nm, sj_nm, thstrm_nm, thstrm_amount, currency, rcept_no
                  FROM fin.dart_financial_accounts
                 WHERE corp_code = %s AND fiscal_year = %s AND fs_div = 'CFS'
                   AND rcept_no = %s AND account_nm = %s
                   AND sj_nm LIKE %s
                 ORDER BY record_index
                """,
                (CORP_CODE, FISCAL_YEAR, RECEIPT, "영업이익", "%포괄손익계산서%"),
            )
            db_rows = cursor.fetchall()

    if len(db_rows) != 1:
        raise SystemExit(f"Supabase에서 조건에 맞는 행이 {len(db_rows)}건입니다. 기대값 1건과 달라 검증을 중단합니다.")

    account, statement, period, amount, currency, receipt = db_rows[0]
    if amount is None or currency != "KRW":
        raise SystemExit(f"금액 또는 통화 확인 실패: amount={amount!s}, currency={currency!r}")
    if not isinstance(amount, Decimal) or amount != amount.to_integral_value():
        raise SystemExit(f"정수 원 단위 금액이 아닙니다: {amount!r}")

    print("[Supabase] 조회 성공")
    print(f"  계정: {account} / 재무제표: {statement} / 기간: {period}")
    print(f"  금액: {amount:,.0f} {currency} / 접수번호: {receipt}")

    from fin_research.retrieval.chroma_retriever import create_chroma

    db = create_chroma(ROOT / "data" / "chroma_multi", COLLECTION)
    total = db._collection.count()
    print(f"[Chroma] 컬렉션 청크 수: {total}")
    if total != 178:
        raise SystemExit(f"Chroma 청크 수가 기존 확인값 178과 다릅니다: {total}")

    question = "심텍의 2025년 연결 영업이익은 얼마인가?"
    hits = db.similarity_search_with_score(question, k=5)
    amount_text = f"{amount:,.0f}"
    matched = []
    for rank, (doc, distance) in enumerate(hits, 1):
        if amount_text in doc.page_content and "영업이익" in doc.page_content:
            matched.append((rank, doc, distance))

    print(f"[Chroma] 질문: {question}")
    print(f"  검색 결과 {len(hits)}건 중 동일 수치와 계정명이 함께 있는 청크: {len(matched)}건")
    for rank, doc, distance in matched:
        print(f"  순위: {rank}, 제목: {doc.metadata.get('title')}")
        print(f"  원문: {doc.metadata.get('source_file')}")
        print(f"  XPath: {doc.metadata.get('source_xpath')}")

    if not matched:
        raise SystemExit("검색 상위 5개 청크에서 Supabase 수치와 계정명을 함께 확인하지 못했습니다.")

    print("\n[성공] Supabase에서 조회한 영업이익 금액이 Chroma 검색 청크에도 존재합니다.")
    print("참고: 이는 하나의 계정에 대한 저장소 간 교차 확인이며 전체 공시의 정확성이나 Researcher 연동 완료를 의미하지 않습니다.")


if __name__ == "__main__":
    main()
