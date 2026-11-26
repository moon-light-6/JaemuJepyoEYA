
# 미적재 기업의 조사 데이터를 실행 중에만 준비

import hashlib
import json
import sys

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from fin_research.ingestion.dart_client import DartClient
from fin_research.ingestion.auto_resolve import fetch_financial_accounts
from fin_research.ingestion.xml_documents import build_xml_documents
from fin_research.llm.client import PROJECT_ROOT
from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.xml_retrieval import build_xml_chroma
from fin_research.tools.doc_search_tool import XMLDocumentSearchTool
from fin_research.tools.financial_account_tool import FinancialAccountTool
from fin_research.verify.accounts import normalize_name


def add_project_root():
    # 기존 scripts 함수를 필요할 때만 불러온다 (서버 시작·업로드 API에는 영향 없음)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.append(str(PROJECT_ROOT))


def _load_script_functions():
    add_project_root()

    from scripts.pipelines.run_pipeline_xml_draft_eval import close_chroma_client
    from scripts.processing.build_xml_chunks_v2 import build_xml_chunks

    return build_xml_chunks, close_chroma_client


def fetch_target_accounts(client: DartClient, target: SearchTarget) -> list[dict]:
    # 정형데이터 조회 후 공시번호가 같은지 확인
    accounts = fetch_financial_accounts(
        client,
        target.corp_code,
        target.fiscal_year,
        target.statement_scope,
    )

    if not accounts:
        raise ValueError("재무제표 계정 데이터가 없습니다.")

    if {row.get("rcept_no") for row in accounts} != {target.rcept_no}:
        raise ValueError("재무제표 계정의 접수번호가 다릅니다.")

    return accounts


@contextmanager
def temporary_index(client: DartClient, company: str, target: SearchTarget, root: Path):
    # DART 원본 → XML 문서 → v2 청크 → 임시 Chroma. 종료·예외 시 Chroma를 닫는다.
    build_xml_chunks, close_chroma_client = _load_script_functions()

    # 1. DART 원본 수집
    zip_path = root / f"{target.rcept_no}.zip"
    client.download_original_zip(target.rcept_no, zip_path)

    # 2. XML 문서 생성
    documents, _ = build_xml_documents(
        zip_path,
        target.corp_code,
        target.fiscal_year,
        target.statement_scope,
        company,
    )

    # 3. 기존 v2 청킹
    chunks = build_xml_chunks(documents, 600, 100)
    chunks_path = (
        target.xml_scope_dir(root)
        / f"{target.rcept_no}_xml_chunks_v2.json"
    )
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    chunks_path.write_text(
        json.dumps(chunks, ensure_ascii=False),
        encoding="utf-8",
    )

    # 4. 임시 Chroma 색인
    chroma_root = root / "chroma"
    chroma_dir = chroma_root / hashlib.sha256(chunks_path.read_bytes()).hexdigest()

    try:
        count = build_xml_chroma(chunks_path, chroma_dir, target.xml_collection_name)

        if count != len(chunks):
            raise ValueError("청크와 색인 수가 일치하지 않습니다.")

        yield {
            "chroma_root": chroma_root,
            "document_count": len(documents),
            "chunk_count": count,
        }

    finally:
        close_chroma_client(chroma_dir)


# 기존 데이터 처리 함수를 재사용하고 임시 데이터 관리 (기존 통합 테스트 스크립트에서 사용)
@contextmanager
def prepare_on_demand(company: str, target: SearchTarget):
    client = DartClient.from_environment()

    with TemporaryDirectory(prefix="research_") as temp_dir:
        root = Path(temp_dir)
        accounts = fetch_target_accounts(client, target)

        with temporary_index(client, company, target, root) as index:
            # 기존 조사 검색 도구 연결
            search_tool = XMLDocumentSearchTool(
                index["chroma_root"],
                target,
                chunk_version=2,
                project_root=root,
            )

            yield {
                "target": target,
                "accounts": accounts,
                "document_count": index["document_count"],
                "chunk_count": index["chunk_count"],
                "search_tool": search_tool,
                "temp_root": root,
                "account_tool": FinancialAccountTool(
                    OnDemandAccountStore(target, accounts)
                ),
            }


class OnDemandAccountStore:
    # OpenDART 계정 데이터를 메모리에서 조회하는 어댑터
    def __init__(self, target: SearchTarget, accounts: list[dict]):
        self.target = target
        self.rows = []

        fs_div = (
            "CFS" if target.statement_scope == "consolidated"
            else "OFS"
        )

        for index, account in enumerate(accounts, start=1):

            # OpenDART 응답에 fs_div가 없으면 조회 요청 기준 사용
            if (
                account.get("corp_code") != target.corp_code
                or str(account.get("bsns_year")) != str(target.fiscal_year)
                or account.get("rcept_no") != target.rcept_no
                or account.get("fs_div") not in (None, fs_div)
                or account.get("reprt_code") != "11011"
            ):
                raise ValueError(
                    f"{index}번 OpenDART 계정의 조회 조건이 다릅니다. "
                    f"corp_code={account.get('corp_code')}, "
                    f"bsns_year={account.get('bsns_year')}, "
                    f"rcept_no={account.get('rcept_no')}, "
                    f"fs_div={account.get('fs_div')}, "
                    f"reprt_code={account.get('reprt_code')}"
                )

            # 기존 FinancialAccountTool에서 사용하는 형식으로 변환
            self.rows.append({
                **account,
                "record_index": index,
                "fiscal_year": target.fiscal_year,
                "fs_div": fs_div,
            })

    # 기존 FinancialAccountTool의 저장소 인터페이스
    def read_financial_accounts(
        self, context, account_ids, account_names
    ):
        if (
            context.corp_code != self.target.corp_code
            or context.year != self.target.fiscal_year
            or context.scope != self.target.statement_scope
            or context.rcept_no != self.target.rcept_no
        ):
            raise ValueError("조사 대상과 계정 데이터가 다릅니다.")

        rows = [
            row for row in self.rows
            if row.get("account_id") in account_ids
            or normalize_name(row.get("account_nm")) in account_names
        ]

        return SimpleNamespace(
            rows=rows,
            truncated=False,
        )
