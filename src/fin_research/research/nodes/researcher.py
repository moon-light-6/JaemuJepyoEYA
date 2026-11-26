"""상위 조사 노드: 기본은 템플릿 숫자·설명 분리와 다중 도구 루프를 사용한다.

- mock_researcher: 검색·LLM 없이 고정 결과 (supervisor·그래프 테스트용)
- make_loop_researcher(): SQL·계산·문서 다중 도구 루프를 상위 조사에 연결하는 어댑터
  · 질문마다 루프를 한 번씩 돌리고 결과를 Finding 하나로 합친다
  · 근거 ID에 '{하위주제}.r{라운드}.q{질문}.' 접두어를 붙여 researcher 간 충돌을 막는다
  · 임베딩 저장소는 공유하고 질문별 재검색 이력은 분리한다
- make_document_researcher(): 기존 문서 전용 조사 어댑터
  · 근거는 실행 전체가 함께 쓰는 EvidenceRegistry(장부)에 등록해 전역 S#로 바꾼다
    (같은 청크는 어느 researcher가 찾아도 같은 S#). 장부가 없으면 '{하위주제}.r{라운드}.q{질문}.' 임시 번호
  · 색인은 로컬 XML(fin_xml_...) → 로컬 PDF(fin_...) → 팀 공용 ChromaDB(XML) 순서로 찾는다
- Hybrid RAG: config에 account_store가 있으면 템플릿의 db_items·db_ratios 숫자를
  DART DB에서 코드가 가져와(research/db_facts.py) Finding 맨 앞에 둔다. 문서 검색은 원인·구조를 찾는다.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from fin_research.companies import load_companies
from fin_research.llm.budget import BudgetExceeded, load_budget_config
from fin_research.llm.client import PROJECT_ROOT
from fin_research.evidence.registry import EvidenceRegistry
from fin_research.research.db_facts import DbFacts, build_db_facts
from fin_research.research.nodes.common import get_account_store, get_budget, get_evidence_registry
from fin_research.retrieval.span_select import select_quote
from fin_research.research.text_checks import drop_unsupported_numbers
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
_CITATION=re.compile(r"\[([^\[\]]*?\bS\d+\b[^\[\]]*?)\]")
_LOCAL_ID=re.compile(r"\bS\d+\b")


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

    색인 선택 (청크가 있는 첫 번째):
      1) 로컬 XML 색인 (data/chroma_multi, scripts/pipelines/run_pipeline_xml.py)
      2) 로컬 PDF 색인
      3) 팀 공용 ChromaDB의 XML 색인 (.env의 CHROMA_SHARED_HOST, 현규 PC) — 로컬에 없을 때만 접속
    모두 없으면 ValueError — 없는 컬렉션을 열면 Chroma가 빈 컬렉션을 새로 만들어
    "검색 결과 0건"으로 조용히 넘어가므로, 열기 전에 먼저 확인한다.
    """

    SOURCES = ("xml", "pdf", "shared")  # 앞에 있을수록 우선

    def __init__(self, persist_directory: Path = CHROMA_DIR,
                 collection_counts: Callable[[], dict[str, int]] | None = None,
                 shared_counts: Callable[[], dict[str, int]] | None = None,
                 shared_client=None):
        self.persist_directory = Path(persist_directory)
        self._tools: dict[SearchTarget, Callable[[str, int], list[dict]]] = {}
        self._sources: dict[SearchTarget, str] = {}
        self._lock = threading.Lock()
        self._collection_counts = collection_counts or self._read_collection_counts
        self._counts: dict[str, int] | None = None
        self._vectorstores = {}
        self._shared_client = shared_client
        self._shared_counts_fn = shared_counts or self._read_shared_counts
        self._shared_counts: dict[str, int] | None = None

    def session(self, target, *, source_format="xml", xml_version=1):
        """동일 검색 정책으로 로컬·공용 저장소를 열고 질문별 상태를 분리한다."""
        return ResearchSearchSession(self, target, source_format, xml_version).search

    def shared_xml_client(self, target):
        """로컬 XML이 없을 때만 선택 공시의 공용 컬렉션 존재 여부를 확인한다."""
        if self._shared_counts is None:
            self._shared_counts = self._shared_counts_fn()
        return self._shared_client if self._shared_counts.get(target.xml_collection_name, 0) else None

    def _read_shared_counts(self) -> dict[str, int]:
        """공용 ChromaDB 컬렉션 → 청크 수. 주소가 없거나 서버가 꺼져 있으면 빈 dict."""
        if self._shared_client is None:
            from fin_research.tools.doc_search_tool import shared_chroma_client

            self._shared_client = shared_chroma_client()
        if self._shared_client is None:
            return {}
        try:
            return {c.name: c.count() for c in self._shared_client.list_collections()}
        except Exception:  # 목록 조회 중 연결이 끊긴 경우도 로컬만으로 판단한다
            return {}

    def _read_collection_counts(self) -> dict[str, int]:
        """컬렉션 이름 → 청크 수. 임베딩 모델 없이 Chroma 메타데이터만 읽는다."""
        if not (self.persist_directory / "chroma.sqlite3").is_file():
            return {}
        import chromadb

        client = chromadb.PersistentClient(path=str(self.persist_directory))
        counts: dict[str, int] = {}
        for collection in client.list_collections():
            name = collection if isinstance(collection, str) else collection.name
            counts[name] = client.get_collection(name).count()
        return counts

    def source_for(self, target: SearchTarget) -> str:
        """이 기업·공시에 쓸 색인 (xml/pdf/shared). 모두 비어 있으면 ValueError."""
        if self._counts is None:
            self._counts = self._collection_counts()
        names = {"xml": target.xml_collection_name, "pdf": target.collection_name}
        for source in ("xml", "pdf"):
            if self._counts.get(names[source], 0) > 0:
                return source
        if self._shared_counts is None:  # 로컬에 없을 때만 공용 서버에 접속한다
            self._shared_counts = self._shared_counts_fn()
        if self._shared_counts.get(names["xml"], 0) > 0:
            return "shared"
        raise ValueError(
            f"검색 색인이 없습니다: {names['xml']} (로컬·공용 ChromaDB) 또는 {names['pdf']}. "
            "scripts/pipelines/run_pipeline_xml.py로 로컬 색인을 만들거나, 공용 ChromaDB(현규 PC)가 "
            "켜져 있는지와 .env의 CHROMA_SHARED_HOST를 확인하세요."
        )

    def source_used(self, target: SearchTarget) -> str | None:
        """search_function으로 이미 정해진 색인 종류 (진행 표시·테스트용)."""
        return self._sources.get(target)

    def search_function(self, target: SearchTarget) -> Callable[[str, int], list[dict]]:
        with self._lock:
            if target not in self._tools:
                from fin_research.tools.doc_search_tool import DocumentSearchTool

                source = self.source_for(target)
                self._sources[target] = source
                if source == "shared":
                    tool = DocumentSearchTool(self.persist_directory, target, "xml", client=self._shared_client)
                else:
                    tool = DocumentSearchTool(self.persist_directory, target, source)
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
                    # 로컬 색인이 없으면 팀 공용 ChromaDB로 (source_for: 로컬 XML → 로컬 PDF → 공용)
                    shared = self.xml_version == 1 and self.cache.source_for(self.target) == "shared"
                    self.tool = XMLDocumentSearchTool(self.cache.persist_directory, self.target,
                        chunk_version=self.xml_version,
                        client_provider=(lambda: self.cache.shared_xml_client(self.target)) if self.xml_version == 1 else None)
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

def rewrite_citations(text:str, mapping:dict[str,str])->str:
    """답변 속 루프 번호를 전역 번호로 한 번에 바꾼다. '[S1, S3]' → '[S12, S4]'.

    mapping에 없는 번호(근거 없이 인용한 번호)는 지우고, 괄호가 비면 괄호째 지운다.
    한 번의 re.sub로 바꿔서 S1→S12, S12→S3처럼 연쇄로 바뀌지 않는다.
    """
    def bracket(match:re.Match)->str:
        ids=[mapping[i] for i in _LOCAL_ID.findall(match.group(1)) if i in mapping]
        return f"[{', '.join(dict.fromkeys(ids))}]" if ids else ""

    return re.sub(r" +([.,])", r"\1", _CITATION.sub(bracket, text)).strip()

def to_candidate(evidence:dict )->dict:
    """루프의 검색 결과-> 장부 등록 형식. XML 위치·제목은 metadata 안에 있을 수 있다."""
    meta=evidence.get("metadata") or {}
    return {**evidence,
        "source_xpath": evidence.get("source_xpath") or meta.get("source_xpath"),
        "title": evidence.get("title") or meta.get("title"),}

def register_citations(
    answer: dict, evidences: list[dict], registry: EvidenceRegistry
) -> tuple[dict[str, str], list[str]]:

    """루프 답변이 인용한 근거를 장부에 등록. ({루프 번호: 전역 번호}, 안내 메시지)."""

    by_id={e.get("evidence_id"): e for e in evidences}
    # evidence_ids에 빠졌어도 본문에 쓴 번호까지 (답변 순서대로)

    # 본문에서는 [S1]·[S1, S3]처럼 대괄호 안의 번호만 인용으로 본다 ('갤럭시 S25' 같은 제품명 제외)
    in_text = [i for group in _CITATION.findall(answer.get("answer") or "") for i in _LOCAL_ID.findall(group)]
    cited=list(dict.fromkeys([*(answer.get("evidence_ids") or []), *in_text]))
    mapping:dict[str,str]={}
    notes:list[str]=[]
    for local_id in cited:
        evidence=by_id.get(local_id)
        if evidence is None:
            notes.append(f"검색 결과에 없는 근거 {local_id}를 인용해 제외했습니다.")
            continue
        try:
            mapping[local_id]=registry.register(to_candidate(evidence))
        except ValidationError:
            notes.append(f"근거 {local_id}에 원문이나 위치(XPath·쪽)가 없어 제외했습니다.")
    return mapping, notes



def merge_loop_results(
    subtopic: SubTopic, round_: int, results: list[tuple[str, dict | None, str | None]],
    registry: EvidenceRegistry | None = None,
) -> Finding:
    """질문별 루프 결과 [(질문, 결과 dict 또는 None, 오류 메시지 또는 None)] → Finding 하나.

    registry가 있으면 근거를 장부에 등록해 전역 S#로 바꾸고, 없으면 임시 번호를 붙인다.
    """
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

        evidences = (result or {}).get("evidences", [])
        # 근거 원문에 없는 숫자(LLM이 더하거나 단위를 바꾼 값)가 든 문장은 뺀다
        kept, dropped = drop_unsupported_numbers(
            answer.get("answer") or "", [question, *(str(e.get("text") or "") for e in evidences)])
        if dropped:
            limitations.append(f"{question} — 근거 원문에 없는 숫자({', '.join(dropped)})를 쓴 문장을 제외")
        if not kept.strip():
            limitations.append(f"{question} — 답변 불가: 근거로 확인되는 문장이 없음")
            continue
        answer = {**answer, "answer": kept}
        if registry is not None:
            mapping, notes = register_citations(answer, evidences, registry)
            parts.append(rewrite_citations(answer["answer"], mapping))
            for global_id in mapping.values():
                if global_id not in ids:  # 같은 근거를 여러 질문이 인용해도 출처는 한 번
                    ids.append(global_id)
                    evidence = registry.require(global_id)
                    # 2단계 근거: 청크에서 이 질문·답변에 맞는 짧은 조각 (보여 주기용, 원문을 자르기만 함)
                    quote = select_quote(evidence.text, question, answer["answer"])
                    sources.append(EvidenceRef.from_evidence(evidence).model_copy(update={"quote": quote}))
            limitations.extend(notes)
        else:
            prefix = f"{subtopic.id}.r{round_}.q{n}"
            summary, prefixed = prefix_sources(answer["answer"], answer.get("evidence_ids") or [], prefix)
            parts.append(summary)
            ids.extend(prefixed)
            by_id = {e.get("evidence_id"): e for e in evidences}
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


# ─────────────────────────── DB 숫자 (Hybrid RAG) ───────────────────────────


def db_spec(subtopic_id: str) -> tuple[list[str], list[str]]:
    """템플릿의 (db_items, db_ratios). 템플릿에 없는 주제(리스크 등)는 빈 목록."""
    from fin_research.research.nodes.brief import load_template  # brief → supervisor → researcher 순환 import 방지

    for spec in load_template():
        if spec["id"] == subtopic_id:
            return list(spec.get("db_items") or []), list(spec.get("db_ratios") or [])
    return [], []


def fetch_db_rows(store, corp_code: str, year: int, fs_div="CFS") -> tuple[list[dict], str]:
    """구형 호출도 지정한 기준만 읽으며 연결 부재를 별도로 대체하지 않는다."""
    return store.rows(corp_code, year, fs_div), fs_div


async def collect_db_facts(
    store, brief: Brief, subtopic: SubTopic, round_: int, registry: EvidenceRegistry | None,
) -> tuple[DbFacts, list[EvidenceRef]]:
    """DB 문장·출처. DB가 없거나 실패하면 limitations에 이유만 남긴다 (문서 검색은 계속)."""
    items, ratio_keys = db_spec(subtopic.id)
    if store is None or not (items or ratio_keys) or not brief.corp_code:
        return DbFacts(), []
    year = max(brief.years)
    try:
        rows, fs_div = await asyncio.to_thread(fetch_db_rows, store, brief.corp_code, year,
                                             "CFS" if brief.scope == "consolidated" else "OFS")
    except Exception as e:  # DB 연결 실패가 조사를 멈추지 않게
        return DbFacts(limitations=[f"DART DB 조회 실패: {type(e).__name__}: {e}"]), []
    if not rows:
        return DbFacts(limitations=[f"DART DB에 {brief.company} {year}년 재무제표가 없음"]), []

    local: dict[str, dict] = {}  # 장부가 없을 때(테스트) 임시 번호와 근거

    def register(candidate: dict) -> str:
        if registry is not None:
            return registry.register(candidate)
        local_id = f"{subtopic.id}.r{round_}.db.S{len(local) + 1}"
        local[local_id] = candidate
        return local_id

    facts = build_db_facts(
        rows, items=items, ratio_keys=ratio_keys, company=brief.company, corp_code=brief.corp_code,
        year=year, fs_div=fs_div, subtopic_id=subtopic.id, register=register,
    )
    sources = []
    for evidence_id in facts.evidence_ids:
        if registry is not None:
            evidence = registry.require(evidence_id)
            # DB·계산 근거는 짧아서 원문 전체를 조각으로 보여 준다 (writer 입력·출처 목록)
            sources.append(EvidenceRef.from_evidence(evidence).model_copy(update={"quote": evidence.text}))
        else:
            c = local[evidence_id]
            sources.append(EvidenceRef(id=evidence_id, source_file=c["source_file"], chunk_id=c["chunk_id"],
                                       title=c["title"], quote=c["text"]))
    return facts, sources


def with_db_facts(finding: Finding, facts: DbFacts, sources: list[EvidenceRef]) -> Finding:
    """구형 호출의 DB 문장도 보존하되 설명이 실패했다면 부분 성공으로 남긴다."""
    if not facts.summary:
        return finding.model_copy(update={"limitations": [*facts.limitations, *finding.limitations]})
    summary = facts.summary if finding.status != "draft" else f"{facts.summary}\n{finding.summary}"
    rag_ids = finding.evidence_ids if finding.status == "draft" else []
    rag_sources = finding.sources if finding.status == "draft" else []
    ids = list(dict.fromkeys([*facts.evidence_ids, *rag_ids]))
    seen = set(facts.evidence_ids)
    return finding.model_copy(update={
        "status": "draft" if finding.status == "draft" and not facts.limitations else "partial",
        "summary": summary,
        "evidence_ids": ids,
        "sources": [*sources, *(s for s in rag_sources if s.id not in seen)],
        "limitations": [*facts.limitations, *finding.limitations],
    })


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
        registry = get_evidence_registry(config)
        db, db_sources = await collect_db_facts(get_account_store(config), brief, subtopic, round_, registry)
        try:
            target = search_target_for(brief)
            search = tools.search_function(target)  # 색인이 없으면 여기서 ValueError
        except ValueError as e:
            return {"findings": [with_db_facts(failed_finding(subtopic, round_, str(e)), db, db_sources)]}
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
        finding = merge_loop_results(subtopic, round_, results, registry)
        return {"findings": [with_db_facts(finding, db, db_sources)]}

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
