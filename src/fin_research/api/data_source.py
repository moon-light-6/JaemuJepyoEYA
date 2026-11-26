"""확정된 공시의 적재 상태를 확인하고 조사·검증에 쓸 데이터와 검색 도구를 준비한다.

정형데이터: Supabase(적재) 또는 OpenDART(미적재). Supabase 연결 실패는 미적재로 보지 않고 중단한다.
검색 색인:  로컬 Chroma → 공용 Chroma → 실행 중에만 쓰는 임시 Chroma.
           공용 Chroma 연결 실패도 임시 색인으로 대체하되 '미적재'와 구분해 사유를 남긴다.
적재 여부는 컬렉션 존재가 아니라 같은 기업·연도·기준·공시의 행·청크 수로 판단한다.
영구 저장소에는 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg2

from fin_research.api.on_demand import (
    OnDemandAccountStore,
    add_project_root,
    fetch_target_accounts,
    temporary_index,
)
from fin_research.ingestion.dart_client import DartClient
from fin_research.research.nodes.researcher import CHROMA_DIR, ResearchSearchSession, SearchToolCache
from fin_research.retrieval.search_target import SearchTarget
from fin_research.tools.doc_search_tool import XMLDocumentSearchTool, shared_chroma_client
from fin_research.tools.financial_account_tool import FinancialAccountTool
from fin_research.verify.accounts import SCOPE_TO_FS_DIV, AccountStore, fetch_rows

SUPABASE_SOURCE = "fin.dart_financial_accounts"
OPENDART_SOURCE = "OpenDART fnlttSinglAcntAll"


class StorageUnavailable(RuntimeError):
    """정형데이터 저장소에 연결하지 못해 적재 여부를 판단할 수 없을 때."""


@dataclass(frozen=True)
class IndexState:
    """status: loaded / empty / absent / mismatch / connection_failed / not_configured"""

    status: str
    chunk_count: int = 0
    detail: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "chunk_count": self.chunk_count, "detail": self.detail}


# ─────────────────────────── 적재 상태 확인 (읽기 전용) ───────────────────────────


def check_structured(target: SearchTarget, fetch: Callable[..., list[dict]] = fetch_rows) -> tuple[list[dict], dict]:
    """(같은 공시의 계정 행, 상태). 연결 실패는 StorageUnavailable."""
    fs_div = SCOPE_TO_FS_DIV[target.statement_scope]
    try:
        rows = fetch(target.corp_code, target.fiscal_year, fs_div)
    except (psycopg2.Error, RuntimeError, OSError) as e:  # 접속 정보가 섞일 수 있어 종류만 남긴다
        raise StorageUnavailable(f"Supabase에 연결하지 못해 적재 여부를 확인할 수 없습니다 ({type(e).__name__}).") from None
    same = [r for r in rows if str(r.get("rcept_no")) == target.rcept_no]
    if same:
        status = "loaded"
    else:
        status = "other_disclosure" if rows else "absent"
    return same, {
        "status": status, "row_count": len(same),
        "other_rcept_nos": sorted({str(r.get("rcept_no")) for r in rows} - {target.rcept_no}),
    }


def check_collection(collection: Any, target: SearchTarget) -> IndexState:
    """컬렉션의 실제 청크가 모두 같은 기업·연도·기준·공시인지 확인한다."""
    count = collection.count()
    if count == 0:
        return IndexState("empty", 0, "컬렉션은 있으나 청크가 없음")
    data = collection.get(include=["metadatas"])
    prefix = f"{target.corp_code}_{target.fiscal_year}_{target.statement_scope}_xml_"
    bad = sum(
        not str(chunk_id).startswith(prefix)
        or (meta or {}).get("fiscal_year") != target.fiscal_year
        or (meta or {}).get("statement_scope") != target.statement_scope
        or not str((meta or {}).get("source_file", "")).startswith(target.rcept_no)
        for chunk_id, meta in zip(data["ids"], data["metadatas"])
    )
    if bad:
        return IndexState("mismatch", count, f"기업·연도·기준·공시가 다른 청크 {bad}개")
    return IndexState("loaded", count)


def _check_client(client: Any, target: SearchTarget) -> IndexState:
    names = {c if isinstance(c, str) else c.name for c in client.list_collections()}
    if target.xml_collection_name not in names:
        return IndexState("absent")
    return check_collection(client.get_collection(target.xml_collection_name), target)


def check_local_index(target: SearchTarget, directory: Path = CHROMA_DIR) -> IndexState:
    if not (Path(directory) / "chroma.sqlite3").is_file():
        return IndexState("absent")
    import chromadb

    try:
        return _check_client(chromadb.PersistentClient(path=str(directory)), target)
    except Exception as e:
        return IndexState("connection_failed", 0, type(e).__name__)


def check_shared_index(target: SearchTarget, connect: Callable[[], Any] = shared_chroma_client) -> tuple[Any, IndexState]:
    """(클라이언트, 상태). shared_chroma_client()의 None을 '주소 없음'과 '연결 실패'로 나눈다."""
    client = connect()
    if client is None:
        if not os.getenv("CHROMA_SHARED_HOST"):
            return None, IndexState("not_configured", 0, "CHROMA_SHARED_HOST 없음")
        return None, IndexState("connection_failed", 0, "공용 Chroma 서버에 연결하지 못함")
    try:
        return client, _check_client(client, target)
    except Exception as e:
        return None, IndexState("connection_failed", 0, type(e).__name__)


# ─────────────────────────── 조사·검증에 주입할 도구 ───────────────────────────


class TemporarySearchToolCache(SearchToolCache):
    """임시 폴더의 v2 청크·색인을 쓰는 검색 도구 캐시. 검색 방식은 기존 도구 그대로다."""

    def __init__(self, chroma_root: Path, project_root: Path):
        super().__init__(chroma_root)
        self.project_root = project_root

    def session(self, target, *, source_format="xml", xml_version=2):
        session = ResearchSearchSession(self, target, source_format, xml_version)
        session.tool = XMLDocumentSearchTool(
            self.persist_directory, target, chunk_version=xml_version, project_root=self.project_root,
        )
        return session.search


class RunAccountStore(AccountStore):
    """이번 실행의 계정 행만 쓰는 검증 기준값.

    주장에 연결/별도 표시가 없으면(unknown) 기존 기본값(연결) 대신 이번 실행의 기준으로 조회한다.
    주장에 명시된 기준은 그대로 따른다.
    """

    def __init__(self, target: SearchTarget, rows: list[dict]):
        self._target = target
        key = (target.corp_code, target.fiscal_year, SCOPE_TO_FS_DIV[target.statement_scope])
        super().__init__(fetch=lambda *args: rows if args == key else [])

    def _scope(self, scope: str) -> str:
        return self._target.statement_scope if scope == "unknown" else scope

    def has_company_data(self, corp_code, fiscal_year, scope="unknown"):
        return super().has_company_data(corp_code, fiscal_year, self._scope(scope))

    def lookup(self, corp_code, fiscal_year, item, scope="unknown", period="current"):
        return super().lookup(corp_code, fiscal_year, item, self._scope(scope), period)


def verify_rows(target: SearchTarget, accounts: list[dict]) -> list[dict]:
    """OpenDART 계정을 검증용 행으로 바꾼다 (기존 적재 코드의 금액 변환 재사용)."""
    add_project_root()
    from scripts.storage.load_dart_accounts import to_number

    rows = []
    for index, account in enumerate(OnDemandAccountStore(target, accounts).rows, start=1):
        if account.get("currency") != "KRW":
            continue
        row = dict(account)
        for name in ("thstrm_amount", "frmtrm_amount"):
            row[name] = to_number(row.get(name), index, name)
        order = row.get("ord")
        row["ord"] = int(order) if order not in (None, "") else None
        rows.append(row)
    if not rows:
        raise ValueError("검증에 사용할 원화 계정 데이터가 없습니다.")
    return rows


@dataclass
class PreparedData:
    """한 실행에서 조사와 검증이 함께 쓰는 데이터."""

    target: SearchTarget
    structured_source: str
    account_store: AccountStore  # 검증용
    account_tool: FinancialAccountTool | None  # 조사용. None이면 기존 PostgresReadOnlyStore
    search_tools: SearchToolCache
    xml_version: int
    info: dict = field(default_factory=dict)


@contextmanager
def prepare_data(company: str, target: SearchTarget, client: DartClient, root: Path, *,
                 fetch: Callable[..., list[dict]] = fetch_rows,
                 local_check: Callable[[SearchTarget], IndexState] = check_local_index,
                 shared_check: Callable[[SearchTarget], tuple[Any, IndexState]] = check_shared_index,
                 fetch_accounts: Callable[..., list[dict]] = fetch_target_accounts,
                 build_index=temporary_index,
                 on_state: Callable[[dict], None] | None = None) -> Iterator[PreparedData]:
    """적재 상태에 따라 데이터를 준비한다. 임시 색인은 구간이 끝나면 닫는다 (폴더 삭제는 호출한 쪽)."""
    rows, structured = check_structured(target, fetch)
    local = local_check(target)
    shared_client, shared = (None, IndexState("not_checked")) if local.status == "loaded" else shared_check(target)

    if local.status == "loaded":
        search_source, reason = "local_chroma", ""
    elif shared.status == "loaded":
        search_source, reason = "shared_chroma", ""
    else:
        search_source = "temporary_chroma"
        # 연결 실패는 '확인하지 못함'이지 '미적재'가 아니다
        reason = "shared_connection_failed" if shared.status == "connection_failed" else "not_loaded"

    structured_loaded = structured["status"] == "loaded"
    structured["source"] = SUPABASE_SOURCE if structured_loaded else OPENDART_SOURCE
    search: dict = {"source": search_source, "fallback_reason": reason,
                    "local": local.to_dict(), "shared": shared.to_dict()}
    if structured_loaded and search_source != "temporary_chroma":
        mode = "loaded"
    else:
        mode = "mixed" if structured_loaded or search_source != "temporary_chroma" else "on_demand"
    info = {"mode": mode, "structured": structured, "search": search}
    if on_state:
        on_state(info)  # 이후 수집이 실패해도 적재 판단은 결과에 남긴다

    account_tool = None
    if not structured_loaded:
        accounts = fetch_accounts(client, target)
        structured["opendart_row_count"] = len(accounts)
        account_tool = FinancialAccountTool(OnDemandAccountStore(target, accounts))
        rows = verify_rows(target, accounts)
    structured["verify_row_count"] = len(rows)

    def prepared(tools: SearchToolCache, xml_version: int) -> PreparedData:
        return PreparedData(target, structured["source"], RunAccountStore(target, rows),
                            account_tool, tools, xml_version, info)

    if search_source == "local_chroma":
        search["chunk_count"] = local.chunk_count
        yield prepared(SearchToolCache(), 1)
    elif search_source == "shared_chroma":
        search["chunk_count"] = shared.chunk_count
        # 로컬 저장소를 열면 빈 컬렉션이 새로 생기므로, 비어 있는 임시 경로를 줘서 공용만 읽는다
        counts = {target.xml_collection_name: shared.chunk_count}
        yield prepared(SearchToolCache(root / "no_local_chroma", shared_client=shared_client,
                                       shared_counts=lambda: counts), 1)
    else:
        with build_index(client, company, target, root) as index:
            search["chunk_count"], search["document_count"] = index["chunk_count"], index["document_count"]
            yield prepared(TemporarySearchToolCache(index["chroma_root"], root), 2)
