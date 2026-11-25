"""실제 OpenDART·Supabase·Chroma·LLM을 호출하는 전체 실행 테스트.

일반 테스트에서는 건너뛴다. 실행 (PowerShell, 프로젝트 루트):
    $env:RUN_E2E = "1"
    .venv/Scripts/python.exe -X utf8 -m pytest tests/integration/test_research_e2e.py -v -s

- 서비스 코드(ResearchService·prepare_data·API)를 그대로 실행한다. patch·mock을 쓰지 않는다.
- 주장 추출은 E2E에서만 small 모델을 지정한다. 나머지는 budget.yaml·models.yaml 기본값이다.
- 통과는 '전체 실행이 끝까지 연결됨'을 뜻한다. 품질은 결과 JSON의 status·quality로 구분한다.
- 결과: data/runs/e2e/*.json
"""
import asyncio
import functools
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.skipif(os.getenv("RUN_E2E") != "1", reason="RUN_E2E=1일 때만 실제 API·LLM 호출")

OUT = ROOT / "data" / "runs" / "e2e"


def make_service(**overrides):
    from fin_research.api.research_service import ResearchOptions, ResearchService

    return ResearchService(options=ResearchOptions(claim_model_key="small"), **overrides)


def snapshot() -> dict:
    """영구 저장소와 임시 폴더 상태 (읽기 전용)."""
    import chromadb
    import psycopg2

    from fin_research.llm.client import PROJECT_ROOT  # .env 로드
    from fin_research.tools.doc_search_tool import shared_chroma_client

    connection = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=8)
    try:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT corp_code, fiscal_year, fs_div, rcept_no, count(*) "
                           "FROM fin.dart_financial_accounts GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4")
            supabase = [list(row) for row in cursor.fetchall()]
    finally:
        connection.close()

    def counts(client):
        return {c.name: c.count() for c in client.list_collections()}

    data = PROJECT_ROOT / "data"
    local = {}
    for path in sorted([data / "chroma_multi", *(data / "chroma_xml_v2").glob("*")]):
        if (path / "chroma.sqlite3").is_file():
            local[path.name] = counts(chromadb.PersistentClient(path=str(path)))
    shared = shared_chroma_client()
    return {
        "supabase": supabase, "local_chroma": local, "shared_chroma": counts(shared) if shared else None,
        "temp": sorted(p.name for p in Path(tempfile.gettempdir()).glob("research_*")),
        "processed_files": sorted(p.relative_to(data).as_posix() for folder in ("raw", "processed")
                                  for p in (data / folder).rglob("*") if p.is_file()),
    }


@pytest.fixture(autouse=True)
def storage_unchanged(request):
    """모든 테스트 전후로 영구 저장소·임시 폴더가 같은지 확인한다."""
    before = snapshot()
    yield
    after = snapshot()
    if before["shared_chroma"] is None or after["shared_chroma"] is None:
        before["shared_chroma"] = after["shared_chroma"] = None  # 공용 서버가 꺼져 비교 불가
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"storage_{request.node.name.replace('[', '_').rstrip(']')}.json").write_text(json.dumps({
        "unchanged": before == after, "supabase_rows": sum(r[-1] for r in after["supabase"]),
        "shared_compared": after["shared_chroma"] is not None, "temp": after["temp"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    assert before == after


def save(name: str, result: dict) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[{name}] status={result['status']} data={json.dumps(result['data'], ensure_ascii=False)}")
    print("  stages:", json.dumps(result["execution"]["stages"], ensure_ascii=False))
    print("  error:", result["error"], "| issues:", [i["code"] for i in result["quality"]["issues"]])
    print("  verify:", (result["verification"] or {}).get("summary"), "| cleanup:", result["cleanup"])
    return result


def wait_for(coroutine):
    """서버와 같은 전용 이벤트 루프에서 실행한다."""
    from fin_research.api.research_routes import run_in_background

    return run_in_background(coroutine).result()


def run(name: str, request: str, service=None, **kwargs) -> dict:
    return save(name, wait_for((service or make_service()).run(request, on_stage=print, **kwargs)))


def assert_cycle(result: dict, mode: str, target: tuple) -> None:
    """전체 실행 연결 확인. 품질 문제는 실패로 보지 않지만 success로 기록되면 안 된다."""
    t = result["target"]
    assert (t["corp_code"], t["fiscal_year"], t["statement_scope"], t["rcept_no"]) == target
    assert result["data"]["mode"] == mode
    assert result["execution"]["completed"], result["error"]
    assert result["report"].strip() and result["findings"] and result["evidences"]
    assert result["cleanup"]["temp_root_removed"] and not result["cleanup"]["close_error"]
    v = result["verification"]
    assert (v["corp_code"], v["fiscal_year"], v["rcept_no"]) == (target[0], target[1], target[3])
    for finding in result["findings"]:
        c = finding["context"]
        assert (c["corp_code"], c["year"], c["scope"], c["rcept_no"]) == target
    for verdict in v["verdicts"]:
        assert verdict["rcept_no"] in (None, target[3])
        assert all(e["source_file"] == v["structured_source"] for e in verdict["evidence"] if e["kind"] == "db")
    if result["quality"]["issues"]:
        assert result["status"] == "partial_success"
    if not v["verdicts"]:
        pytest.xfail("전체 실행은 끝났지만 주장별 판정이 0건 (주장 추출 단계)")


def test_unloaded_company_through_api():
    """미적재 기업: POST → GET으로 전체 실행."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fin_research.api.research_routes import ResearchJobs, create_router

    app = FastAPI()
    app.include_router(create_router(ResearchJobs(make_service)))
    client = TestClient(app)
    accepted = client.post("/api/research", json={"request": "CJ대한통운의 2025년 연결 기준 재무 상태를 분석해줘."})
    assert accepted.status_code == 202
    statuses, stages = [], []
    deadline = time.monotonic() + 45 * 60
    while time.monotonic() < deadline:
        job = client.get(f"/api/research/{accepted.json()['id']}").json()
        if not statuses or statuses[-1] != job["status"]:
            statuses.append(job["status"])
        if job["stage"] and (not stages or stages[-1] != job["stage"]):
            stages.append(job["stage"])
            print(job["stage"], flush=True)
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(1)
    result = save("unloaded_cj_logistics", {**job["result"], "api_statuses": statuses, "api_stages": stages})
    assert job["status"] == result["status"] and "running" in statuses
    assert_cycle(result, "on_demand", ("00113410", 2025, "consolidated", "20260316001417"))


def test_loaded_company():
    """적재 기업: Supabase와 기존 Chroma 재사용."""
    result = run("loaded_seojin", "서진시스템의 2025년 연결 기준 수익성과 재무 상태를 분석해줘.")
    assert_cycle(result, "loaded", ("00838005", 2025, "consolidated", "20260323001543"))


def test_loaded_company_separate_scope():
    """별도 기준만 공시한 적재 기업: 검증도 별도 기준으로 조회."""
    result = run("loaded_leeno_separate", "리노공업의 2025년 별도 기준 재무 상태를 분석해줘.")
    assert_cycle(result, "loaded", ("00369657", 2025, "separate", "20260318000182"))


@pytest.mark.parametrize("name, request_text, missing", [
    ("ask_no_company", "2025년 연결 기준 재무 상태를 분석해줘.", ["company"]),
    ("ask_partial_name", "에코프로머티리얼의 2025년 연결 기준 재무 상태를 분석해줘.", ["company"]),
    ("ask_same_name", "에스엠의 2025년 연결 기준 재무 상태를 분석해줘.", ["company"]),
    ("ask_year", "CJ대한통운의 연결 기준 재무 상태를 분석해줘.", ["year"]),
    ("ask_scope", "CJ대한통운의 2025년 재무 상태를 분석해줘.", ["scope"]),
], ids=["no_company", "partial_name", "same_name", "year", "scope"])
def test_clarification(name, request_text, missing):
    result = run(name, request_text)
    assert result["status"] == "needs_clarification" and result["missing"] == missing and result["question"]
    print("  question:", result["question"], "| candidates:", len(result["candidates"]))
    if name == "ask_same_name":
        assert len(result["candidates"]) > 1
    assert result["data"] is None and result["report"] == ""


def test_similar_name_is_not_replaced_by_loaded_company():
    """에코프로머티가 적재된 에코프로로 바뀌지 않는다 (식별까지만 실행)."""
    from fin_research.api.company_resolver import resolve_request
    from fin_research.ingestion.dart_client import DartClient

    resolved = wait_for(resolve_request("에코프로머티의 2025년 별도 기준 재무 상태를 분석해줘.",
                                        client=DartClient.from_environment()))
    assert (resolved.company, resolved.target.corp_code) == ("에코프로머티", "01311408")


def test_missing_disclosure():
    result = run("no_disclosure", "CJ대한통운의 2026년 연결 기준 재무 상태를 분석해줘.")
    assert result["status"] == "failed" and result["error"]["code"] == "disclosure_not_found"
    assert result["execution"]["failed_stage"] == "identify"


def test_exception_during_run_cleans_up():
    """실제 원본 수집·임시 색인까지 끝낸 뒤 조사 단계에서 예외가 나는 경우."""
    roots = []

    async def failing_research(resolved, data, options, on_step):
        root = data.search_tools.project_root
        assert root.is_dir() and any(root.rglob("chroma.sqlite3"))
        # 검색 연결을 연 상태(파일 잠금)에서 예외를 낸다
        search = data.search_tools.session(resolved.target, source_format="xml", xml_version=2)
        assert await asyncio.to_thread(search, "차입금 현황", 3)
        roots.append(root)
        raise RuntimeError("E2E 강제 예외")

    result = run("exception_cleanup", "CJ대한통운의 2025년 연결 기준 재무 상태를 분석해줘.",
                 make_service(research=failing_research))
    assert result["status"] == "failed" and result["error"]["stage"] == "research"
    assert result["cleanup"]["temp_root_removed"] and not result["cleanup"]["close_error"]
    assert roots and not roots[0].exists()


def test_shared_chroma_failure_uses_temporary_index():
    """공용 Chroma 연결 실패 → 임시 색인 + 사유 기록 (정형데이터는 Supabase 그대로)."""
    from fin_research.api.data_source import check_shared_index, prepare_data
    from fin_research.api.research_service import remove_tree
    from fin_research.ingestion.dart_client import DartClient
    from fin_research.retrieval.search_target import SearchTarget

    target = SearchTarget("00838005", 2025, "consolidated", "20260323001543")
    root = Path(tempfile.mkdtemp(prefix="research_"))
    try:
        with prepare_data("서진시스템", target, DartClient.from_environment(), root,
                          shared_check=lambda t: check_shared_index(t, connect=lambda: None)) as data:
            assert data.info["mode"] == "mixed" and data.account_tool is None
            assert data.info["search"]["shared"]["status"] == "connection_failed"
            assert data.info["search"]["fallback_reason"] == "shared_connection_failed"
            assert data.info["search"]["chunk_count"] > 0
            assert data.search_tools.session(target)("차입금 현황", 3)
            print(json.dumps(data.info, ensure_ascii=False))
    finally:
        assert remove_tree(root)["temp_root_removed"]


def test_supabase_failure_stops():
    """Supabase 연결 실패(실제로 열리지 않는 포트) → 미적재로 보지 않고 중단."""
    import psycopg2

    from fin_research.api.data_source import prepare_data

    def unreachable(corp_code, fiscal_year, fs_div):
        psycopg2.connect("postgresql://user:pw@127.0.0.1:1/db", connect_timeout=2)

    result = run("supabase_down", "서진시스템의 2025년 연결 기준 재무 상태를 분석해줘.",
                 make_service(prepare=functools.partial(prepare_data, fetch=unreachable)))
    assert result["status"] == "failed" and result["error"]["code"] == "storage_unavailable"
    assert result["error"]["stage"] == "prepare" and result["cleanup"]["temp_root_removed"]
