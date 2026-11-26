"""전체 실행 서비스 단위 테스트 (LLM·OpenDART·Supabase·Chroma 호출 없음).

실제 외부 호출 테스트는 tests/integration/test_research_e2e.py에 있다.
"""
import asyncio
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fin_research import companies  # noqa: E402
from fin_research.api import data_source as ds  # noqa: E402
from fin_research.api import research_service as rs  # noqa: E402
from fin_research.api.company_resolver import (  # noqa: E402
    ClarificationNeeded,
    DisclosureNotFound,
    ResolvedRequest,
    match_corps,
    resolve_request,
)
from fin_research.api.on_demand import OnDemandAccountStore  # noqa: E402
from fin_research.api.research_routes import ResearchJobs, create_router  # noqa: E402
from fin_research.ingestion.auto_resolve import CorpInfo  # noqa: E402
from fin_research.ingestion.dart_client import ReportCandidate  # noqa: E402
from fin_research.research.nodes.clarify import ClarifyExtraction  # noqa: E402
from fin_research.research.state import Finding, QuestionFinding, ResearchNumber  # noqa: E402
from fin_research.retrieval.search_target import SearchTarget  # noqa: E402
from fin_research.verify.nodes.verify_claim import judge  # noqa: E402
from fin_research.verify.state import Claim, Verdict  # noqa: E402

TARGET = SearchTarget("00113410", 2025, "consolidated", "20260316001417")
CORPS = [
    CorpInfo("00113410", "CJ대한통운", "000120"),
    CorpInfo("00536541", "에코프로", "086520"),
    CorpInfo("01311408", "에코프로머티", "450080"),
    CorpInfo("00199252", "HLB", "028300"),
    CorpInfo("00336817", "HLB제약", "047920"),
    CorpInfo("00260930", "에스엠", "041510"),
    CorpInfo("00999999", "에스엠", ""),
]


class FakeDart:
    api_key = "k"

    def __init__(self, reports=("20260316001417",)):
        self.reports = reports

    def search_business_reports(self, corp_code, year, start, end):
        return [ReportCandidate("", corp_code, year, f"사업보고서 ({year}.12)", r, "", "", "") for r in self.reports]

    def _get_json(self, url, params):
        return {"status": "000", "ceo_nm": "대표", "induty_code": "49"}


def resolve(request="q", *, names=(), years=(2025,), scopes=("consolidated",), corp_code=None, dart=None, focus=()):
    async def extract(request, budget):
        return ClarifyExtraction(company_mentions=list(names), explicit_years=list(years), recent_n_years=None,
                                 focus=list(focus), scopes=list(scopes))

    return asyncio.run(resolve_request(request, client=dart or FakeDart(), corp_code=corp_code,
                                       extract=extract, load_corps=lambda: CORPS))


# ─────────────────────────── 실행 단위 기업 정보 ───────────────────────────


def company(name, code="00000001"):
    return {"name": name, "corp_code": code, "reports": {"2025": {"rcept_no": "2" * 14}}}


def test_run_companies_do_not_touch_yaml_cache():
    before = companies.load_companies()
    with companies.use_run_companies([company("임시")]):
        assert [c["name"] for c in companies.load_companies()] == ["임시"]
        assert companies.find_companies("삼성전자") == []
        companies.load_companies()[0]["name"] = "변경"  # 복사본이라 원본·다음 실행에 남지 않는다
    assert companies.load_companies() is before
    assert companies.find_companies("임시") == []


def test_run_companies_cleared_on_error():
    with pytest.raises(RuntimeError):
        with companies.use_run_companies([company("임시")]):
            raise RuntimeError
    assert len(companies.load_companies()) > 1


def test_run_companies_are_isolated_between_concurrent_runs():
    async def one(name):
        with companies.use_run_companies([company(name)]):
            seen = []
            for _ in range(5):
                await asyncio.sleep(0)
                seen.append(companies.load_companies()[0]["name"])
                seen.append((await asyncio.to_thread(companies.load_companies))[0]["name"])
            return set(seen)

    async def main():
        return await asyncio.gather(*(one(f"기업{n}") for n in range(8)), asyncio.to_thread(companies.supported_names))

    *runs, outside = asyncio.run(main())
    assert runs == [{f"기업{n}"} for n in range(8)]
    assert "삼성전자" in outside


# ─────────────────────────── 요청 → 기업·공시 ───────────────────────────


def test_resolves_unloaded_company():
    got = resolve(names=["CJ대한통운"], focus=["수익성"])
    assert got.target == TARGET and got.stock_code == "000120"
    assert "CJ대한통운의 2025년 연결 기준" in got.graph_request and "수익성" in got.graph_request


@pytest.mark.parametrize("name, code", [("에코프로머티", "01311408"), ("HLB제약", "00336817"), ("에코프로", "00536541")])
def test_partial_names_do_not_resolve_to_other_company(name, code):
    assert resolve(names=[name]).target.corp_code == code


def test_unknown_name_is_not_replaced_by_partial_match():
    with pytest.raises(ClarificationNeeded) as e:
        resolve(names=["에코프"])
    assert e.value.missing == ["company"]
    assert {c["corp_name"] for c in e.value.candidates} == {"에코프로", "에코프로머티"}


def test_same_name_returns_candidates_even_if_only_one_is_listed():
    with pytest.raises(ClarificationNeeded) as e:
        resolve(names=["에스엠"])
    assert [c["corp_code"] for c in e.value.candidates] == ["00260930", "00999999"]
    assert e.value.candidates[0]["ceo"] == "대표"


def test_same_name_narrowed_only_by_explicit_code():
    assert resolve("에스엠 041510 2025년", names=["에스엠"]).target.corp_code == "00260930"
    assert resolve(names=["에스엠"], corp_code="00999999").target.corp_code == "00999999"


def test_conflicting_or_unknown_code_asks():
    with pytest.raises(ClarificationNeeded):
        resolve(names=["CJ대한통운"], corp_code="00260930")
    with pytest.raises(ClarificationNeeded):
        resolve(names=["CJ대한통운"], corp_code="12345678")


def test_code_only_request():
    assert resolve("000120 2025년 연결").target.corp_code == "00113410"
    assert match_corps("00113410", CORPS)[0].corp_name == "CJ대한통운"


@pytest.mark.parametrize("kwargs, missing", [
    ({"names": []}, ["company"]),
    ({"names": ["CJ대한통운"], "years": ()}, ["year"]),
    ({"names": ["CJ대한통운"], "years": (2024, 2025)}, ["year"]),
    ({"names": ["CJ대한통운"], "scopes": ()}, ["scope"]),
    ({"names": ["CJ대한통운"], "scopes": ("consolidated", "separate")}, ["scope"]),
    ({"names": [], "years": (), "scopes": ()}, ["company", "year", "scope"]),
    ({"names": ["CJ대한통운", "HLB"]}, ["company"]),
])
def test_missing_conditions_are_asked(kwargs, missing):
    with pytest.raises(ClarificationNeeded) as e:
        resolve(**kwargs)
    assert e.value.missing == missing


def test_missing_disclosure():
    with pytest.raises(DisclosureNotFound):
        resolve(names=["CJ대한통운"], dart=FakeDart(()))
    with pytest.raises(DisclosureNotFound):
        resolve(names=["CJ대한통운"], years=(2999,))


# ─────────────────────────── 적재 상태 ───────────────────────────


def test_structured_states():
    rows = [{"rcept_no": TARGET.rcept_no}, {"rcept_no": "20250101000000"}]
    same, info = ds.check_structured(TARGET, lambda *a: rows)
    assert len(same) == 1 and info["status"] == "loaded"
    assert ds.check_structured(TARGET, lambda *a: rows[1:])[1]["status"] == "other_disclosure"
    assert ds.check_structured(TARGET, lambda *a: [])[1]["status"] == "absent"


@pytest.mark.parametrize("error", [psycopg2.OperationalError("password=secret"), RuntimeError("x"), OSError("x")])
def test_structured_connection_failure_is_not_absent(error):
    def fetch(*args):
        raise error

    with pytest.raises(ds.StorageUnavailable) as e:
        ds.check_structured(TARGET, fetch)
    assert "secret" not in str(e.value)


def collection(n, **override):
    meta = {"fiscal_year": 2025, "statement_scope": "consolidated", "source_file": f"{TARGET.rcept_no}_1.xml", **override}
    return SimpleNamespace(
        name=TARGET.xml_collection_name, count=lambda: n,
        get=lambda include: {"ids": [f"00113410_2025_consolidated_xml_{i:03d}" for i in range(n)], "metadatas": [meta] * n})


def test_index_judged_by_chunks_not_by_collection():
    assert ds.check_collection(collection(0), TARGET).status == "empty"
    assert ds.check_collection(collection(3), TARGET) == ds.IndexState("loaded", 3)
    assert ds.check_collection(collection(3, source_file="20250101000000_1.xml"), TARGET).status == "mismatch"
    assert ds.check_collection(collection(3, statement_scope="separate"), TARGET).status == "mismatch"


def test_shared_index_connection_failure_vs_absent(monkeypatch):
    monkeypatch.setenv("CHROMA_SHARED_HOST", "host")
    assert ds.check_shared_index(TARGET, lambda: None)[1].status == "connection_failed"
    monkeypatch.delenv("CHROMA_SHARED_HOST")
    assert ds.check_shared_index(TARGET, lambda: None)[1].status == "not_configured"
    empty = SimpleNamespace(list_collections=lambda: [])
    assert ds.check_shared_index(TARGET, lambda: empty)[1].status == "absent"

    def broken():
        raise ConnectionError

    assert ds.check_shared_index(TARGET, lambda: SimpleNamespace(list_collections=broken))[1].status == "connection_failed"


def account(**extra):
    return {"corp_code": TARGET.corp_code, "bsns_year": "2025", "rcept_no": TARGET.rcept_no, "reprt_code": "11011",
            "sj_div": "IS", "account_id": "ifrs-full_Revenue", "account_nm": "매출액", "currency": "KRW",
            "thstrm_amount": "1,000", "frmtrm_amount": "900", "ord": "1", **extra}


DB_ROW = {"rcept_no": TARGET.rcept_no, "fs_div": "CFS", "sj_div": "IS", "account_id": "ifrs-full_Revenue",
          "account_nm": "매출액", "thstrm_amount": 1000.0, "frmtrm_amount": 900.0, "ord": 1}


def prepare(tmp_path, *, db, local="absent", shared="absent"):
    calls = []

    @contextmanager
    def build(client, name, target, root):
        calls.append("index")
        yield {"chroma_root": root / "chroma", "chunk_count": 5, "document_count": 2}
        calls.append("closed")

    def accounts(client, target):
        calls.append("opendart")
        return [account()]

    with ds.prepare_data("CJ대한통운", TARGET, None, tmp_path, fetch=lambda *a: db,
                         local_check=lambda t: ds.IndexState(local, 5 if local == "loaded" else 0),
                         shared_check=lambda t: (object(), ds.IndexState(shared, 7 if shared == "loaded" else 0)),
                         fetch_accounts=accounts, build_index=build) as data:
        pass
    return data, calls


def test_loaded_company_uses_existing_storage(tmp_path):
    data, calls = prepare(tmp_path, db=[DB_ROW], shared="loaded")
    assert calls == [] and data.info["mode"] == "loaded"
    assert data.structured_source == ds.SUPABASE_SOURCE and data.account_tool is None and data.xml_version == 1
    assert data.info["search"]["source"] == "shared_chroma"
    # 로컬 영구 저장소를 열지 않으므로 빈 컬렉션도 만들지 않는다
    assert data.search_tools.persist_directory == tmp_path / "no_local_chroma"
    assert data.search_tools.shared_xml_client(TARGET) is not None


def test_unloaded_company_uses_run_data_once(tmp_path):
    data, calls = prepare(tmp_path, db=[])
    assert calls == ["opendart", "index", "closed"] and data.info["mode"] == "on_demand"
    assert data.structured_source == ds.OPENDART_SOURCE and data.xml_version == 2
    assert data.info["search"]["fallback_reason"] == "not_loaded"
    assert data.account_store.lookup(TARGET.corp_code, 2025, "매출액").amount == 1000


def test_shared_failure_falls_back_with_reason(tmp_path):
    data, calls = prepare(tmp_path, db=[DB_ROW], shared="connection_failed")
    assert calls == ["index", "closed"] and data.info["mode"] == "mixed"
    assert data.structured_source == ds.SUPABASE_SOURCE
    assert data.info["search"]["fallback_reason"] == "shared_connection_failed"


def test_on_demand_store_rejects_other_disclosure():
    with pytest.raises(ValueError):
        OnDemandAccountStore(TARGET, [account(rcept_no="20250101000000")])


# ─────────────────────────── 별도 기준 검증 ───────────────────────────


def test_unmarked_claim_uses_run_scope():
    target = SearchTarget("00369657", 2025, "separate", "20260318000182")
    store = ds.RunAccountStore(target, [{**DB_ROW, "rcept_no": target.rcept_no, "fs_div": "OFS"}])
    claim = Claim(id="C1", text="매출액은 1000원", item="매출액", value=1000, unit="원", year=2025)
    verdict = judge(claim, store, target.corp_code, 2025, target.rcept_no)
    assert verdict.label == "match"
    # 주장에 연결이 명시돼 있으면 별도 값과 비교하지 않는다
    marked = judge(claim.model_copy(update={"scope": "consolidated"}), store, target.corp_code, 2025, target.rcept_no)
    assert marked.label == "not_found"

    resolved = ResolvedRequest("리노공업", "058470", target, "", "")
    out = rs.verification_output({"claims": [claim], "verdicts": [verdict]}, resolved, ds.OPENDART_SOURCE)
    assert "연결" not in out["verdicts"][0]["explanation"] and "별도 기준으로 적용" in out["verdicts"][0]["explanation"]
    assert [e["source_file"] for e in out["verdicts"][0]["evidence"] if e["kind"] == "db"] == [ds.OPENDART_SOURCE]


# ─────────────────────────── 전체 실행 ───────────────────────────


RESOLVED = ResolvedRequest("CJ대한통운", "000120", TARGET, "사업보고서 (2025.12)", "후보 1건")


class Env:
    """가짜 의존성. 임시 폴더·기업 정보가 단계 사이에서 어떻게 쓰이는지 기록한다."""

    def __init__(self, *, fail=None, finding="draft", verdicts=True):
        self.fail, self.finding, self.verdicts = fail, finding, verdicts
        self.root, self.closed, self.seen = None, False, {}

    def service(self):
        return rs.ResearchService(client_factory=lambda: None, resolve=self.resolve, prepare=self.prepare,
                                  research=self.research, verify=self.verify)

    async def resolve(self, request, *, client, budget, corp_code):
        if isinstance(self.fail, dict) and "resolve" in self.fail:
            raise self.fail["resolve"]
        return RESOLVED

    @contextmanager
    def prepare(self, name, target, client, root, *, on_state):
        self.root = root
        (root / "data.bin").write_bytes(b"x")
        on_state({"mode": "on_demand"})
        self._raise("prepare")
        try:
            yield ds.PreparedData(target, ds.OPENDART_SOURCE, None, None, None, 2, {"mode": "on_demand"})
        finally:
            self.closed = True

    def _raise(self, stage):
        if isinstance(self.fail, dict) and stage in self.fail:
            raise self.fail[stage]

    async def research(self, resolved, data, options, on_step):
        self.seen["research"] = companies.load_companies()
        self._raise("research")
        finding = Finding(subtopic_id="profitability", status=self.finding, summary="s")
        evidence = {"evidence_id": "S1", "source_file": ds.SUPABASE_SOURCE}
        return {"report": "# 제목\n\n## 요약\n\n본문", "findings": [finding]}, [evidence], {}

    async def verify(self, resolved, data, report, root, options):
        self.seen["verify"] = companies.load_companies()
        self._raise("verify")
        claim = Claim(id="C1", text="t", item="매출액", value=1, unit="원")
        return {"claims": [claim] if self.verdicts else [], "errors": [],
                "verdicts": [Verdict(claim_id="C1", label="match")] if self.verdicts else []}, {}

    def run(self):
        return asyncio.run(self.service().run("요청"))


def test_full_cycle_success():
    env = Env()
    out = env.run()
    assert out["status"] == "success" and out["execution"]["completed"] and out["quality"]["complete"]
    assert env.seen["research"] == env.seen["verify"] == (RESOLVED.company_config,)
    assert out["evidences"][0]["source_file"] == ds.OPENDART_SOURCE
    assert out["verification"]["verdicts"][0]["label"] == "match"
    assert env.closed and not env.root.exists() and out["cleanup"]["temp_root_removed"]


@pytest.mark.parametrize("env, code", [
    (Env(finding="partial"), "research_incomplete"), (Env(verdicts=False), "no_verdicts"),
])
def test_connected_but_incomplete_is_partial(env, code):
    out = env.run()
    assert out["status"] == "partial_success" and out["execution"]["completed"]
    assert code in [i["code"] for i in out["quality"]["issues"]]


def test_silently_missing_claims_are_reported():
    """추출이 일부만 반환하고 오류 없이 끝나도 success가 되지 않는다."""
    context = {"company": "A", "corp_code": TARGET.corp_code, "year": 2025, "scope": "consolidated",
               "rcept_no": TARGET.rcept_no}
    numbers = [ResearchNumber(metric=m, value=v, unit="원", context=context)
               for m, v in (("revenue", "1234567"), ("operating_profit", "-2345678"), ("total_assets", "999"))]
    finding = Finding(subtopic_id="profitability", status="draft", summary="s",
                      question_results=[QuestionFinding(question="q", numbers=numbers)])
    report = "매출액은 1,234,567원, 영업이익은 -2,345,678원"  # 자산총계는 보고서에 없음
    claims = [{"id": "C1", "item": "매출액", "value": 1234567}]
    assert rs.unchecked_metrics([finding], report, claims) == ["영업이익"]
    issues = rs.quality_issues({"findings": [finding], "report": report},
                               {"claims": claims, "verdicts": [{"claim_id": "C1", "label": "match"}]})
    assert [i["code"] for i in issues] == ["claims_incomplete"]


def test_verify_failure_keeps_report():
    env = Env(fail={"verify": RuntimeError("key=secret")})
    out = env.run()
    assert out["status"] == "partial_success" and out["report"] and not out["execution"]["completed"]
    assert out["error"] == {"code": "internal_error", "stage": "verify", "message": "RuntimeError"}
    assert env.closed and not env.root.exists()


@pytest.mark.parametrize("stage", ["prepare", "research"])
def test_failure_cleans_up(stage):
    env = Env(fail={stage: ds.StorageUnavailable("연결 실패")})
    out = env.run()
    assert out["status"] == "failed" and out["error"]["stage"] == stage
    assert out["error"]["code"] == "storage_unavailable"
    assert not env.root.exists() and out["cleanup"]["temp_root_removed"]
    assert len(companies.load_companies()) > 1  # 기업 정보도 남지 않는다


def test_clarification_result():
    env = Env(fail={"resolve": ClarificationNeeded("어느 기준인가요?", ["scope"], [{"corp_name": "A"}])})
    out = env.run()
    assert out["status"] == "needs_clarification" and out["missing"] == ["scope"] and out["candidates"]
    assert env.root is None


def test_cancelled_run_cleans_up():
    env = Env()

    async def slow(*args):
        await asyncio.sleep(60)

    async def main():
        service = env.service()
        service.research = slow
        task = asyncio.create_task(service.run("요청"))
        while env.root is None:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert env.closed and not env.root.exists()


def test_context_mismatch_is_detected():
    ok = SimpleNamespace(need_clarification=False, corp_code=TARGET.corp_code, years=[2025], scope="consolidated")
    rs.check_research_context(ok, RESOLVED)
    for bad in ({"scope": "separate"}, {"years": [2024]}, {"corp_code": "00000000"}):
        with pytest.raises(rs.ContextMismatch):
            rs.check_research_context(SimpleNamespace(**{**vars(ok), **bad}), RESOLVED)
    with pytest.raises(rs.ContextMismatch):
        rs.check_finding_context(SimpleNamespace(corp_code=TARGET.corp_code, year=2025, scope="consolidated",
                                                 rcept_no="1" * 14), RESOLVED)


def test_report_pages_by_section():
    pages = rs.report_pages("# 제목\n\n## 요약\n\n가\n\n## 결론\n\n나\n")
    assert [p["page_number"] for p in pages] == [1, 2, 3]
    assert pages[1]["text"] == "## 요약\n\n가"


# ─────────────────────────── API ───────────────────────────


def client_for(service_factory):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(create_router(ResearchJobs(service_factory)))
    return TestClient(app)


def wait(client, job_id):
    for _ in range(200):
        job = client.get(f"/api/research/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError(job)


@pytest.mark.parametrize("env, status", [
    (Env(), "success"), (Env(finding="partial"), "partial_success"),
    (Env(fail={"prepare": ValueError("x")}), "failed"),
    (Env(fail={"resolve": ClarificationNeeded("?", ["year"])}), "needs_clarification"),
])
def test_api_job_states(env, status):
    client = client_for(env.service)
    accepted = client.post("/api/research", json={"request": "CJ대한통운 2025년 연결"})
    assert accepted.status_code == 202 and accepted.json()["status"] == "queued"
    job = wait(client, accepted.json()["id"])
    assert job["status"] == status and job["result"]["status"] == status and job["finished_at"]


def test_jobs_share_one_event_loop_and_run_one_at_a_time():
    loops, active, peak = set(), 0, 0

    class Service:
        async def run(self, request, **kwargs):
            nonlocal active, peak
            loops.add(id(asyncio.get_running_loop()))
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {"status": "success"}

    jobs = ResearchJobs(Service)
    ids = [jobs.submit("q") for _ in range(4)]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(jobs.get(i)["status"] != "success" for i in ids):
        time.sleep(0.01)
    assert [jobs.get(i)["status"] for i in ids] == ["success"] * 4
    assert len(loops) == 1 and peak == 1


def test_concurrent_services_keep_their_own_company():
    """서비스 실행 둘이 동시에 돌아도 기업 정보가 섞이지 않는다."""
    async def one(name, code):
        resolved = ResolvedRequest(name, "", SearchTarget(code, 2025, "consolidated", "2" * 14), "", "")
        env = Env()

        async def resolve(request, **kwargs):
            return resolved

        async def research(*args):
            for _ in range(5):
                await asyncio.sleep(0.005)
                env.seen.setdefault("names", set()).add((await asyncio.to_thread(companies.load_companies))[0]["name"])
            return await env.research(*args)

        service = env.service()
        service.resolve, service.research = resolve, research
        out = await service.run("q")
        return out["target"]["company"], env.seen["names"], env.seen["verify"][0]["name"]

    async def main():
        return await asyncio.gather(*(one(f"기업{n}", f"0000000{n}") for n in range(4)))

    assert asyncio.run(main()) == [(f"기업{n}", {f"기업{n}"}, f"기업{n}") for n in range(4)]


def test_api_unexpected_error_marks_failed():
    def broken():
        raise RuntimeError("secret")

    client = client_for(broken)
    job = wait(client, client.post("/api/research", json={"request": "q"}).json()["id"])
    assert job["status"] == "failed" and "secret" not in str(job)


def test_api_validation_and_unknown_id():
    client = client_for(Env().service)
    assert client.post("/api/research", json={"request": ""}).status_code == 422
    assert client.post("/api/research", json={"request": "q", "corp_code": "12"}).status_code == 422
    assert client.get("/api/research/00000000-0000-0000-0000-000000000000").status_code == 404


def test_server_keeps_existing_endpoints():
    from fin_research.api.server import app

    assert set(app.openapi()["paths"]) == {
        "/api/health", "/api/jobs", "/api/jobs/{job_id}", "/api/jobs/{job_id}/preview",
        "/api/jobs/{job_id}/extract", "/api/research", "/api/research/{job_id}",
    }


def test_service_code_has_no_test_doubles():
    for path in (ROOT / "src/fin_research/api").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "unittest" not in text and "mock" not in text.lower() and "patch(" not in text, path.name
