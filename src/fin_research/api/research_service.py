"""사용자 요청 하나로 전체 과정을 실행한다.

    기업·공시 확정 → 적재 확인 → 데이터 준비 → 기존 Research 그래프 → 기존 검증 그래프 → 정리

- 조사·검증 내부 로직은 바꾸지 않고 공개 인터페이스(주입 인자·config)만 쓴다.
- 기업·연도·기준·공시는 ResolvedRequest 하나로 고정하고, 두 그래프의 결과와 다르면 실패로 본다.
- 실행이 끝까지 갔는지(execution)와 결과 품질(quality)을 나눠 기록한다.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from fin_research.api.company_resolver import (
    ClarificationNeeded,
    DisclosureNotFound,
    ResolvedRequest,
    resolve_request,
)
from fin_research.api.data_source import (
    SUPABASE_SOURCE,
    PreparedData,
    StorageUnavailable,
    prepare_data,
)
from fin_research.companies import use_run_companies
from fin_research.evidence.registry import EvidenceRegistry
from fin_research.ingestion.dart_client import DartClient, DartError
from fin_research.llm.budget import Budget
from fin_research.research.graph import build_graph as build_research_graph
from fin_research.research.graph import run_config as research_config
from fin_research.research.nodes.researcher import make_loop_researcher
from fin_research.research.nodes.supervisor import latest_by_subtopic
from fin_research.tools.financial_catalog import CATALOG, canonical_metric
from fin_research.verify.accounts import find_alias
from fin_research.verify.graph import build_graph as build_verify_graph
from fin_research.verify.graph import run_config as verify_config

STAGES = ("identify", "prepare", "research", "verify", "cleanup")


class ContextMismatch(RuntimeError):
    """그래프가 확정 조건과 다른 기업·연도·기준·공시를 사용했을 때."""


@dataclass(frozen=True)
class ResearchOptions:
    """None이면 budget.yaml·models.yaml의 기본값."""

    questions_per_subtopic: int | None = None
    loop_rounds: int | None = None
    max_rounds: int | None = None
    claim_model_key: str | None = None


# ─────────────────────────── 그래프 실행 ───────────────────────────


async def run_research_graph(resolved: ResolvedRequest, data: PreparedData, options: ResearchOptions,
                             on_step: Callable[[str], None]) -> tuple[dict, list[dict], dict]:
    """(state, 근거 장부, 사용량). use_run_companies 안에서 실행한다."""
    budget, registry = Budget.from_config(), EvidenceRegistry()
    researcher = make_loop_researcher(
        search_tools=data.search_tools, account_tool=data.account_tool,
        source_format="xml", xml_version=data.xml_version,
        questions_per_subtopic=options.questions_per_subtopic, loop_rounds=options.loop_rounds,
    )
    extra = {"max_rounds": options.max_rounds} if options.max_rounds else {}
    config = research_config(budget, registry, research_rcept_no=resolved.target.rcept_no, write_report=True, **extra)

    state: dict = {}
    checked = False
    async for state in build_research_graph(researcher).astream(
        {"request": resolved.graph_request, "findings": []}, config, stream_mode="values",
    ):
        if not checked and state.get("clarify") is not None:  # 조건이 다르면 조사 비용을 쓰기 전에 멈춘다
            check_research_context(state["clarify"], resolved)
            checked = True
        if state.get("step"):
            on_step(state["step"])
    for finding in state.get("findings") or []:
        if finding.context is not None:
            check_finding_context(finding.context, resolved)
    return state, [e.model_dump(mode="json") for e in registry.values()], budget.summary()


def check_research_context(clarify: Any, resolved: ResolvedRequest) -> None:
    t = resolved.target
    if clarify.need_clarification:
        raise ContextMismatch(f"Research 그래프가 확정 조건을 인식하지 못했습니다: {clarify.question}")
    if (clarify.corp_code, clarify.years, clarify.scope) != (t.corp_code, [t.fiscal_year], t.statement_scope):
        raise ContextMismatch("Research 그래프의 기업·연도·기준이 확정 조건과 다릅니다.")


def check_finding_context(context: Any, resolved: ResolvedRequest) -> None:
    t = resolved.target
    if (context.corp_code, context.year, context.scope, context.rcept_no) != (
            t.corp_code, t.fiscal_year, t.statement_scope, t.rcept_no):
        raise ContextMismatch("조사 결과의 기업·연도·기준·공시가 확정 조건과 다릅니다.")


def report_pages(report: str) -> list[dict]:
    """보고서를 섹션(## 제목)마다 한 쪽으로. 기존 검증 그래프의 추출 JSON 형식 (PDF 쪽번호가 아님)."""
    sections = [s.strip() for s in re.split(r"(?m)^(?=## )", report) if s.strip()]
    return [{"page_number": n, "text": text, "is_blank": False} for n, text in enumerate(sections, start=1)]


async def run_verify_graph(resolved: ResolvedRequest, data: PreparedData, report: str, root: Path,
                           options: ResearchOptions) -> tuple[dict, dict]:
    """기존 검증 그래프를 한 번 실행한다. (결과, 사용량)"""
    pages_path = root / "report_pages.json"
    pages_path.write_text(json.dumps({"pages": report_pages(report)}, ensure_ascii=False), encoding="utf-8")
    budget = Budget.from_config()
    extra = {"claim_model_key": options.claim_model_key} if options.claim_model_key else {}
    result = await build_verify_graph().ainvoke(
        {"pdf_path": str(pages_path), "company": resolved.company},
        verify_config(budget, account_store=data.account_store, **extra),
    )
    t = resolved.target
    if (result.get("corp_code"), result.get("fiscal_year"), result.get("rcept_no")) != (
            t.corp_code, t.fiscal_year, t.rcept_no):
        raise ContextMismatch("검증 그래프의 기업·연도·공시가 확정 조건과 다릅니다.")
    return result, budget.summary()


# ─────────────────────────── 결과 정리 (순수 함수) ───────────────────────────


def to_plain(value: Any) -> Any:
    """Pydantic 모델을 JSON으로 바꿀 수 있는 값으로."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return value


def label_source(value: Any, source: str) -> Any:
    """기존 코드가 고정으로 적는 DB 이름을 실제 사용한 정형데이터 출처로 바꾼다."""
    if isinstance(value, dict):
        return {k: source if k == "source_file" and v == SUPABASE_SOURCE else label_source(v, source)
                for k, v in value.items()}
    if isinstance(value, list):
        return [label_source(v, source) for v in value]
    return value


def verification_output(result: dict, resolved: ResolvedRequest, source: str) -> dict:
    out = label_source(to_plain({k: result.get(k) for k in (
        "company", "corp_code", "fiscal_year", "rcept_no", "claims", "verdicts", "summary", "errors")}), source)
    out["statement_scope"] = resolved.target.statement_scope
    out["structured_source"] = source
    if resolved.target.statement_scope == "separate":
        # 기준 표시가 없는 주장은 RunAccountStore가 별도 기준으로 조회했으므로 안내 문구도 맞춘다
        unknown = {c["id"] for c in out["claims"] or [] if c["scope"] == "unknown"}
        for verdict in out["verdicts"] or []:
            if verdict["claim_id"] in unknown:
                verdict["explanation"] = verdict["explanation"].replace("연결 기준으로 가정", "별도 기준으로 적용")
    return out


def unchecked_metrics(findings: list, report: str, claims: list[dict]) -> list[str]:
    """보고서에 정형데이터 금액이 그대로 적혔는데 추출된 주장이 없는 지표 이름.

    주장 추출은 일부만 반환해도 오류 없이 끝날 수 있어, 누락을 따로 확인한다.
    """
    stated = {n.metric for f in findings for q in f.question_results for n in q.numbers
              if n.metric in CATALOG and format(Decimal(n.value), ",f") in report}
    specs = [find_alias(c["item"]) for c in claims]
    extracted = {canonical_metric(s.key) for s in specs if s}
    return [CATALOG[m]["label"] for m in CATALOG if m in stated - extracted]


def quality_issues(state: dict, verification: dict | None) -> list[dict]:
    """전체 실행과 별개로, 결과가 완전하지 않은 이유. 하나라도 있으면 부분 성공."""
    issues: list[dict] = []

    def add(code: str, detail: Any) -> None:
        issues.append({"code": code, "detail": detail})

    latest = latest_by_subtopic(state.get("findings") or [])
    incomplete = {k: f.status for k, f in latest.items() if f.status != "draft"}
    if not latest or incomplete:
        add("research_incomplete", incomplete or "조사 결과 없음")
    if state.get("errors"):
        add("research_errors", list(state["errors"]))
    if verification is None:
        add("verification_not_completed", "검증 그래프가 끝나지 않음")
        return issues
    if verification.get("errors"):
        add("verification_errors", verification["errors"])
    claims, verdicts = verification.get("claims") or [], verification.get("verdicts") or []
    if not verdicts:
        add("no_verdicts", "주장별 판정 결과가 없음")
    missed = unchecked_metrics(list(latest.values()), state.get("report") or "", claims)
    if missed:
        add("claims_incomplete", missed)
    no_value = [c["id"] for c in claims if c["value"] is None]
    if no_value:
        add("claim_value_missing", no_value)
    for label in ("not_found", "unverifiable"):
        ids = [v["claim_id"] for v in verdicts if v["label"] == label]
        if ids:
            add(f"verdict_{label}", ids)
    return issues


def safe_error(e: BaseException) -> str:
    """API 응답에 넣을 오류. 키·접속 정보가 섞일 수 있는 외부 예외는 종류만 남긴다."""
    known = (DartError, StorageUnavailable, ContextMismatch, ValueError, FileNotFoundError)
    return f"{type(e).__name__}: {e}" if isinstance(e, known) else type(e).__name__


def error_code(e: BaseException) -> str:
    for kind, code in ((DisclosureNotFound, "disclosure_not_found"), (StorageUnavailable, "storage_unavailable"),
                       (ContextMismatch, "context_mismatch"), (DartError, "opendart_error")):
        if isinstance(e, kind):
            return code
    return "internal_error"


def remove_tree(root: Path, attempts: int = 5) -> dict:
    """임시 폴더 삭제. Windows에서 파일 잠금이 늦게 풀릴 수 있어 몇 번 다시 시도한다."""
    error = ""
    for attempt in range(attempts):
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except OSError as e:
            error = type(e).__name__
            time.sleep(0.5 * (attempt + 1))
            continue
        break
    removed = not root.exists()
    return {"temp_root_removed": removed, "error": "" if removed else error}


# ─────────────────────────── 실행 ───────────────────────────


@dataclass
class ResearchService:
    """전체 실행. 외부 의존성은 생성자로 바꿀 수 있다 (기본값이 실제 서비스 경로)."""

    client_factory: Callable[[], DartClient] = DartClient.from_environment
    resolve: Callable[..., Any] = resolve_request
    prepare: Callable[..., Any] = prepare_data
    research: Callable[..., Any] = run_research_graph
    verify: Callable[..., Any] = run_verify_graph
    options: ResearchOptions = field(default_factory=ResearchOptions)

    async def run(self, request: str, *, corp_code: str | None = None,
                  on_stage: Callable[[str], None] | None = None) -> dict:
        """자연어 요청 하나 → 결과 dict. 예외를 던지지 않고 status로 알린다."""
        run = _Run(request, on_stage or (lambda stage: None))
        root: Path | None = None
        verification: dict | None = None
        state: dict = {}
        try:
            async with run.stage("identify"):
                client = self.client_factory()
                resolved = await self.resolve(request, client=client, budget=Budget.from_config(), corp_code=corp_code)
                run.result["target"] = resolved.to_dict()

            root = Path(tempfile.mkdtemp(prefix="research_"))
            manager = self.prepare(resolved.company, resolved.target, client, root,
                                   on_state=lambda info: run.result.update(data=info))
            async with run.stage("prepare"):
                data = await asyncio.to_thread(manager.__enter__)
                run.result["data"] = data.info
            try:
                with use_run_companies([resolved.company_config]):
                    async with run.stage("research"):
                        state, evidences, usage = await self.research(
                            resolved, data, self.options, lambda step: run.notify(f"research:{step}"))
                        run.result.update(
                            report=state.get("report") or "",
                            findings=label_source(to_plain(state.get("findings") or []), data.structured_source),
                            evidences=label_source(evidences, data.structured_source),
                        )
                        run.result["usage"]["research"] = usage
                        if not run.result["report"].strip():
                            raise ValueError("보고서가 생성되지 않았습니다.")
                    async with run.stage("verify"):
                        raw, usage = await self.verify(resolved, data, run.result["report"], root, self.options)
                        verification = verification_output(raw, resolved, data.structured_source)
                        run.result["verification"] = verification
                        run.result["usage"]["verify"] = usage
            finally:
                try:
                    await asyncio.to_thread(manager.__exit__, None, None, None)
                except Exception as e:  # 닫기 실패가 이미 만든 보고서를 버리게 하지 않는다
                    run.close_error = type(e).__name__
        except ClarificationNeeded as e:
            run.result.update(status="needs_clarification", question=e.question,
                              missing=e.missing, candidates=e.candidates)
        except Exception as e:
            run.result["error"] = {"code": error_code(e), "stage": run.failed_stage, "message": safe_error(e)}
        finally:
            if root is not None:
                async with run.stage("cleanup"):
                    run.result["cleanup"] = await asyncio.to_thread(remove_tree, root)
        return run.finish(state, verification)


class _Run:
    """실행 하나의 단계 기록과 최종 상태 판정."""

    def __init__(self, request: str, notify: Callable[[str], None]):
        self.notify = notify
        self.failed_stage: str | None = None
        self.close_error = ""
        self.stages: dict[str, dict] = {name: {"status": "skipped"} for name in STAGES}
        self.result: dict[str, Any] = {
            "run_id": uuid4().hex, "status": "failed", "request": request, "target": None, "data": None,
            "report": "", "findings": [], "evidences": [], "verification": None, "usage": {}, "error": None,
            "cleanup": {"temp_root_removed": None, "error": ""},
        }

    @asynccontextmanager
    async def stage(self, name: str) -> AsyncIterator[None]:
        self.notify(name)
        started = time.monotonic()
        try:
            yield
        except ClarificationNeeded:
            self.stages[name] = {"status": "needs_clarification", "seconds": round(time.monotonic() - started, 1)}
            raise
        except BaseException as e:
            self.failed_stage = name
            self.stages[name] = {"status": "failed", "seconds": round(time.monotonic() - started, 1),
                                 "error": safe_error(e)}
            raise
        self.stages[name] = {"status": "ok", "seconds": round(time.monotonic() - started, 1)}

    def finish(self, state: dict, verification: dict | None) -> dict:
        result = self.result
        result["cleanup"]["close_error"] = self.close_error
        if result["cleanup"]["temp_root_removed"] is False or self.close_error:
            self.stages["cleanup"]["status"] = "failed"
        completed = all(self.stages[name]["status"] == "ok" for name in STAGES)
        result["execution"] = {"completed": completed, "failed_stage": self.failed_stage, "stages": self.stages}
        if result["status"] == "needs_clarification":
            result["quality"] = {"complete": None, "issues": []}
            return result
        issues = quality_issues(state, verification) if result["report"].strip() else []
        result["quality"] = {"complete": bool(result["report"].strip()) and not issues, "issues": issues}
        if not result["report"].strip():
            result["status"] = "failed"
        else:
            result["status"] = "success" if completed and not issues else "partial_success"
        return result
