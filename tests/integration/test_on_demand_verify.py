
# 미적재 기업의 생성 보고서 검증 통합 테스트

import asyncio
import json
import sys
import textwrap

from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.api.on_demand import OnDemandAccountStore
from fin_research.ingestion.dart_client import DartClient
from fin_research.ingestion.auto_resolve import fetch_financial_accounts
from fin_research.retrieval.search_target import SearchTarget
from fin_research.llm.budget import Budget
from fin_research.verify.accounts import AccountStore
from fin_research.verify.graph import build_graph, run_config

from scripts.storage.load_dart_accounts import to_number
from scripts.demo.demo_verify import print_result, to_json


async def main():
    company = "CJ대한통운"

    target = SearchTarget(
        "00113410",
        2025,
        "consolidated",
        "20260316001417",
    )

    run_dir = PROJECT_ROOT / "data/runs"

    # 1. 기존 생성 보고서 불러오기
    research_path = run_dir / "cj_on_demand_research.json"

    research = json.loads(
        research_path.read_text(encoding="utf-8")
    )

    report = research.get("report")

    if not report or not report.strip():
        raise ValueError("생성된 보고서가 없습니다.")

    # 2. 기존 검증 그래프의 입력 형식으로 변환
    # 실제 PDF 페이지가 아닌 테스트용 가상 페이지
    pages = []
    blocks = textwrap.wrap(
        report,
        width=1200,
        replace_whitespace=False,
        drop_whitespace=False,
    )

    for index, block in enumerate(blocks, start=1):
        pages.append({
            "page_number": index,
            "text": block,
            "is_blank": False,
        })

    pages_path = run_dir / "cj_on_demand_verify_input.json"

    pages_path.write_text(
        json.dumps(
            {"pages": pages},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 3. OpenDART 정형데이터 조회
    accounts = fetch_financial_accounts(
        DartClient.from_environment(),
        target.corp_code,
        target.fiscal_year,
        target.statement_scope,
    )

    # 기존에 테스트한 서빙 어댑터 재사용
    adapter = OnDemandAccountStore(target, accounts)

    rows = []

    for index, account in enumerate(adapter.rows, start=1):
        if account.get("currency") != "KRW":
            continue

        row = dict(account)

        # 기존 적재 코드의 금액 변환 함수 재사용
        for field in ("thstrm_amount", "frmtrm_amount"):
            row[field] = to_number(
                row.get(field),
                index,
                field,
            )

        order = row.get("ord")
        row["ord"] = (
            int(order) if order not in (None, "") else None
        )

        rows.append(row)

    if not rows:
        raise ValueError("검증에 사용할 계정 데이터가 없습니다.")

    # 4. Supabase 대신 메모리 계정 데이터 전달
    def fetch_rows(corp_code, fiscal_year, fs_div):
        if (
            corp_code == target.corp_code
            and int(fiscal_year) == target.fiscal_year
            and fs_div == "CFS"
        ):
            return rows

        return []

    account_store = AccountStore(fetch=fetch_rows)

    # 5. 테스트용 기업 설정
    company_config = ({
        "name": company,
        "corp_code": target.corp_code,
        "reports": {
            str(target.fiscal_year): {
                "rcept_no": target.rcept_no,
            },
        },
    },)

    # 6. 기존 검증 그래프 실행
    budget = Budget.from_config()

    with (
        patch(
            "fin_research.companies.load_companies",
            return_value=company_config,
        ),
        patch(
            "fin_research.verify.nodes.claim_extract.extraction_limits",
            return_value=(1500, 2),
        ),
        patch(
            "fin_research.verify.nodes.verify_claim.DB_SOURCE",
            "OpenDART fnlttSinglAcntAll",
        ),
    ):
        result = await build_graph().ainvoke(
            {
                "pdf_path": str(pages_path),
                "company": company,
            },
            run_config(
                budget,
                account_store=account_store,
                claim_model_key="small",
            ),
        )

    # 7. 기존 결과 출력 기능 재사용
    print_result(result)

    result["model_info"] = {"key": "small"}

    output = run_dir / "cj_on_demand_verify.json"

    output.write_text(
        json.dumps(
            to_json(result),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n=== 검증 통합 테스트 결과 ===")
    print("OpenDART 계정 수:", len(accounts))
    print("검증용 계정 수:", len(rows))
    print("추출된 주장 수:", len(result.get("claims", [])))
    print("검증 결과 수:", len(result.get("verdicts", [])))
    print("판정 요약:", result.get("summary"))
    print("오류:", result.get("errors", []))
    print("결과 저장:", output)


if __name__ == "__main__":
    asyncio.run(main())
