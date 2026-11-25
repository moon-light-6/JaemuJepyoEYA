"""researcher 노드: 하위 주제 하나를 조사해 Finding 하나를 돌려준다.

- mock_researcher: 검색·LLM 없이 고정 결과 (supervisor·그래프 테스트용)
- make_loop_researcher(): SQL·계산·문서 다중 도구 루프를 상위 조사에 연결하는 어댑터
  · 질문마다 루프를 한 번씩 돌리고 결과를 Finding 하나로 합친다
  · 근거 ID에 '{하위주제}.r{라운드}.q{질문}.' 접두어를 붙여 researcher 간 충돌을 막는다
  · 임베딩 저장소는 공유하고 질문별 재검색 이력은 분리한다
- make_document_researcher(): 기존 문서 전용 조사 어댑터
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable
from pathlib import Path

from langchain_core.runnables import RunnableConfig

from fin_research.companies import load_companies
from fin_research.llm.budget import BudgetExceeded, load_budget_config
from fin_research.llm.client import PROJECT_ROOT
from fin_research.research.nodes.common import get_budget
from fin_research.research.state import (
    Brief,
    EvidenceRef,
    Finding,
    ResearcherInput,
    ResearcherOutput,
    SubTopic,
)
from fin_research.retrieval.search_target import SearchTarget

CHROMA_DIR = PROJECT_ROOT / "data" / "chroma_multi"
BUDGET_EXCEEDED_MARK = "[예산 초과]"  # supervisor가 재조사를 멈추는 신호
_SOURCE_ID = re.compile(r"\[(S\d+)\]")


# ─────────────────────────── 검색 대상·도구 ───────────────────────────


def search_target_for(brief: Brief) -> SearchTarget:
    """Brief의 기업·연도 → SearchTarget. 연도가 여러 개면 최신 연도만 (TODO: 연도별 검색)."""
    company = next((c for c in load_companies() if c.get("corp_code") == brief.corp_code), None)
    if company is None:
        raise ValueError(f"companies.yaml에 corp_code {brief.corp_code}가 없습니다.")
    year = max(brief.years)
    report = (company.get("reports") or {}).get(str(year)) or {}
    rcept_no = report.get("rcept_no")
    if not rcept_no:
        raise ValueError(f"{company['name']} {year}년 접수번호(rcept_no)가 없어 검색 색인을 찾을 수 없습니다.")
    return SearchTarget(
        corp_code=brief.corp_code, fiscal_year=year,
        statement_scope=brief.scope, rcept_no=rcept_no,
    )


class SearchToolCache:
    """실행 하나에서 기업별 검색 도구를 한 번만 만들고, 검색은 직렬화한다.

    병렬 researcher가 각자 도구를 만들면 KURE 모델이 여러 번 메모리에 올라간다.
    임베딩 계산도 스레드 간 공유가 안전하지 않아 검색 자체는 잠금으로 순서대로 한다 (LLM 대기가 더 길다).
    """

    def __init__(self, persist_directory: Path = CHROMA_DIR):
        self.persist_directory = Path(persist_directory)
        self._tools: dict[SearchTarget, Callable[[str, int], list[dict]]] = {}
        self._lock = threading.Lock()
        self._vectorstores: dict[tuple, object] = {}

    def session(self, target: SearchTarget, *, source_format: str = "xml", xml_version: int = 1):
        """임베딩 저장소만 공유하고 재검색 이력은 질문별로 분리한 검색 함수를 만든다."""
        return ResearchSearchSession(self, target, source_format, xml_version).search

    def search_function(self, target: SearchTarget) -> Callable[[str, int], list[dict]]:
        with self._lock:
            if target not in self._tools:
                from fin_research.tools.doc_search_tool import DocumentSearchTool

                tool = DocumentSearchTool(self.persist_directory, target)
                search_lock = threading.Lock()

                def search(question: str, top_k: int) -> list[dict]:
                    with search_lock:
                        return tool.search(question, top_k)

                self._tools[target] = search
            return self._tools[target]


class ResearchSearchSession:
    """질문마다 별도의 XML 재검색 상태를 두고 GPU 검색은 공용 잠금으로 직렬화한다."""

    def __init__(self, cache: SearchToolCache, target: SearchTarget, source_format: str, xml_version: int):
        """저장소나 모델은 실제 문서 검색이 필요할 때까지 불러오지 않는다."""
        self.cache, self.target = cache, target
        self.source_format, self.xml_version = source_format, xml_version
        self.tool = None

    @property
    def collection_name(self):
        """검색 이력에 실제 PDF 또는 XML 컬렉션 이름을 기록한다."""
        return self.target.xml_collection_name if self.source_format == "xml" else self.target.collection_name

    @property
    def last_search_info(self):
        """질문별 도구가 마지막으로 실행한 검색 전략을 반환한다."""
        return dict(getattr(self.tool, "last_search_info", {}))

    def search(self, question: str, top_k: int = 5):
        """질문별 이력을 유지하면서 이미 로드된 벡터 저장소와 임베딩 모델을 재사용한다."""
        from fin_research.tools.doc_search_tool import DocumentSearchTool, XMLDocumentSearchTool

        with self.cache._lock:
            if self.tool is None:
                if self.source_format == "xml":
                    self.tool = XMLDocumentSearchTool(self.cache.persist_directory, self.target,
                                                       chunk_version=self.xml_version)
                else:
                    self.tool = DocumentSearchTool(self.cache.persist_directory, self.target)
            key = (self.target, self.source_format, self.xml_version)
            if key in self.cache._vectorstores:
                self.tool._vectorstore = self.cache._vectorstores[key]
            try:
                return self.tool.search(question, top_k)
            finally:
                if self.tool._vectorstore is not None:
                    self.cache._vectorstores[key] = self.tool._vectorstore


# ─────────────────────────── 결과 합치기 (순수 함수) ───────────────────────────


def prefix_sources(answer: str, evidence_ids: list[str], prefix: str) -> tuple[str, list[str]]:
    """'[S1]' → '[{prefix}.S1]'. 답변 속 표기와 ID 목록을 같이 바꾼다."""
    summary = _SOURCE_ID.sub(lambda m: f"[{prefix}.{m.group(1)}]", answer)
    return summary, [f"{prefix}.{i}" for i in evidence_ids]


def merge_loop_results(
    subtopic: SubTopic, round_: int, results: list[tuple[str, dict | None, str | None]]
) -> Finding:
    """질문별 루프 결과 [(질문, 결과 dict 또는 None, 오류 메시지 또는 None)] → Finding 하나."""
    parts: list[str] = []
    ids: list[str] = []
    sources: list[EvidenceRef] = []
    limitations: list[str] = []

    for n, (question, result, error) in enumerate(results, start=1):
        if error:
            limitations.append(f"{question} — {error}")
            continue
        answer = (result or {}).get("findings") or {}
        if answer.get("status") != "draft":
            reasons = "; ".join(answer.get("limitations") or []) or "근거 부족"
            limitations.append(f"{question} — 답변 불가: {reasons}")
            continue

        prefix = f"{subtopic.id}.r{round_}.q{n}"
        summary, prefixed = prefix_sources(answer["answer"], answer.get("evidence_ids") or [], prefix)
        parts.append(summary)
        ids.extend(prefixed)
        by_id = {e.get("evidence_id"): e for e in (result or {}).get("evidences", [])}
        for raw_id, new_id in zip(answer.get("evidence_ids") or [], prefixed):
            e = by_id.get(raw_id, {})
            sources.append(EvidenceRef(
                id=new_id, source_file=e.get("source_file"),
                pdf_page=e.get("pdf_page"), chunk_id=e.get("chunk_id"),
            ))
        limitations.extend(answer.get("limitations") or [])

    return Finding(
        subtopic_id=subtopic.id,
        status="draft" if parts else "insufficient_evidence",
        summary="\n".join(parts) if parts else f"{subtopic.title}: 확보한 근거로 답할 수 없었습니다.",
        evidence_ids=ids,
        sources=sources,
        limitations=limitations,
        round=round_,
    )


def failed_finding(subtopic: SubTopic, round_: int, reason: str) -> Finding:
    return Finding(
        subtopic_id=subtopic.id, status="insufficient_evidence",
        summary=f"{subtopic.title}: 조사하지 못했습니다.", limitations=[reason], round=round_,
    )


# ─────────────────────────── 노드 ───────────────────────────


async def mock_researcher(inp: ResearcherInput, config: RunnableConfig) -> ResearcherOutput:
    """고정 결과. 검색·LLM 없이 supervisor·그래프 흐름을 확인할 때 쓴다."""
    s = inp["subtopic"]
    prefix = f"{s.id}.r{inp['round']}.q1"
    return {"findings": [Finding(
        subtopic_id=s.id, status="draft", summary=f"{s.title} 조사 결과 (mock) [{prefix}.S1]",
        evidence_ids=[f"{prefix}.S1"], sources=[EvidenceRef(id=f"{prefix}.S1")], round=inp["round"],
    )]}


def make_document_researcher(
    *,
    search_tools: SearchToolCache | None = None,
    questions_per_subtopic: int | None = None,
    loop_rounds: int | None = None,
    top_k: int = 5,
    loop_fn: Callable[..., dict] | None = None,
):
    """실제 루프를 쓰는 researcher 노드를 만든다. 그래프 하나(실행 하나)에 하나씩 만든다."""
    research = load_budget_config().get("research") or {}
    n_questions = questions_per_subtopic or research.get("questions_per_subtopic") or 2
    rounds = loop_rounds or research.get("loop_max_iterations") or 3
    tools = search_tools or SearchToolCache()
    if loop_fn is None:
        from fin_research.research.loop.graph import run_research_loop as loop_fn

    async def loop_researcher(inp: ResearcherInput, config: RunnableConfig) -> ResearcherOutput:
        subtopic, brief, round_ = inp["subtopic"], inp["brief"], inp["round"]
        try:
            target = search_target_for(brief)
        except ValueError as e:
            return {"findings": [failed_finding(subtopic, round_, str(e))]}

        search = tools.search_function(target)
        budget = get_budget(config)
        results: list[tuple[str, dict | None, str | None]] = []
        for question in subtopic.questions[:n_questions]:
            try:
                # 루프는 동기 함수 → 스레드에서 실행해 이벤트 루프(다른 researcher)를 막지 않는다
                result = await asyncio.to_thread(
                    loop_fn, question, tools.persist_directory, top_k, None, rounds,
                    search_function=search, budget=budget, target=target,
                )
                results.append((question, result, None))
            except BudgetExceeded as e:
                results.append((question, None, f"{BUDGET_EXCEEDED_MARK} {e}"))
                break
            except Exception as e:  # 한 질문 실패가 하위 주제 전체를 죽이지 않게
                results.append((question, None, f"조사 실패: {type(e).__name__}: {e}"))
        return {"findings": [merge_loop_results(subtopic, round_, results)]}

    return loop_researcher


def make_loop_researcher(*, search_tools=None, questions_per_subtopic=None, loop_rounds=None,
                         top_k=5, loop_fn=None, tool_loop_fn=None, account_tool=None, model=None,
                         source_format="xml", xml_version=1, progress=None):
    """기본은 다중 도구 조사로 연결하고 loop_fn을 명시한 기존 문서 어댑터도 보존한다."""
    if loop_fn is not None:
        # 기존 문서 전용 루프와 테스트 주입 방식은 명시적으로 선택할 수 있게 보존한다.
        return make_document_researcher(search_tools=search_tools, questions_per_subtopic=questions_per_subtopic,
                                        loop_rounds=loop_rounds, top_k=top_k, loop_fn=loop_fn)
    from fin_research.research.multi_tool_adapter import make_tool_researcher
    return make_tool_researcher(search_tools=search_tools, questions_per_subtopic=questions_per_subtopic,
                                loop_rounds=loop_rounds, top_k=top_k, loop_fn=tool_loop_fn,
                                account_tool=account_tool, model=model, source_format=source_format,
                                xml_version=xml_version, progress=progress)
