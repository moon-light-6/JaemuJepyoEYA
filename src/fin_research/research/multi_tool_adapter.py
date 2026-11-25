"""상위 Researcher와 SQL·계산·문서 루프의 입력·출력 및 부분 재조사를 연결한다."""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from langchain_core.runnables import RunnableConfig

from fin_research.llm.budget import BudgetExceeded, load_budget_config
from fin_research.llm.client import PROJECT_ROOT
from fin_research.progress import report_progress
from fin_research.research.nodes.common import get_budget
from fin_research.research.state import EvidenceRef, Finding, QuestionFinding, ResearchNumber
from fin_research.tools.financial_models import FinancialContext

_GROUP = re.compile(r"\[([^\[\]]*\bS\d+\b[^\[\]]*)\]")
_ID = re.compile(r"\bS\d+\b")
_BUDGET_MARK = "[예산 초과]"


def resolve_context(brief, *, rcept_no=None, project_root=PROJECT_ROOT) -> FinancialContext:
    """기업·단일 연도·연결/별도와 접수번호를 고정하고 모호한 기준은 보류한다."""
    from fin_research.companies import load_companies

    years = sorted(set(brief.years))
    if len(years) != 1:
        raise ValueError("다중 도구 조사는 한 번에 한 사업연도만 지원합니다. 다년 비교는 연도별로 요청하세요.")
    company = next((c for c in load_companies() if c.get("corp_code") == brief.corp_code), None)
    if company is None:
        raise ValueError("기업 고유번호를 companies.yaml에서 확인하지 못했습니다.")
    report = (company.get("reports") or {}).get(str(years[0])) or {}
    receipt = rcept_no or report.get("rcept_no")
    if not receipt:
        # 자동 다운로드·DART 재조회 없이, 선택한 기준의 기존 처리 기록만 확인한다.
        scope_dir = project_root / "data/processed/xml/dart" / brief.corp_code / str(years[0]) / brief.scope
        receipts = []
        for path in scope_dir.glob("*_xml_pipeline.json"):
            match = re.fullmatch(r"(\d{14})_xml_pipeline.json", path.name)
            if not match:
                continue
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if (data.get("source_format") == "xml" and data.get("statement_scope") == brief.scope
                    and str(data.get("source_xml", "")).startswith(match.group(1))):
                receipts.append(match.group(1))
        if len(set(receipts)) != 1:
            raise ValueError("접수번호를 하나로 확정하지 못했습니다. 기업 설정 또는 --rcept-no를 지정하세요.")
        receipt = receipts[0]
    return FinancialContext(company=company["name"], corp_code=brief.corp_code, year=years[0],
                            scope=brief.scope, rcept_no=receipt)


def remap_citations(text: str, mapping: dict[str, str]) -> str:
    """단일·묶음 인용의 출처 번호를 바꾸고 근거에 없는 인용은 거부한다."""
    def replace(match):
        """대괄호 안의 S 번호만 변환해 제품명이나 일반 문장은 유지한다."""
        def replace_id(identifier):
            """검색·조회한 출처만 상위 전역 번호로 바꾼다."""
            key = identifier.group(0)
            if key not in mapping:
                raise ValueError("답변에 확보하지 않은 출처가 포함되어 있습니다.")
            return mapping[key]
        return "[" + _ID.sub(replace_id, match.group(1)) + "]"
    return _GROUP.sub(replace, text)


def convert_result(question: str, result: dict, context: FinancialContext, prefix: str, round_: int) -> QuestionFinding:
    """원문을 복제하지 않고 숫자·위치·실행 이력·부분 상태를 상위 자료형으로 변환한다."""
    if FinancialContext.model_validate(result.get("context")) != context:
        raise ValueError("조사 결과의 기업·기간·기준·접수번호가 요청과 다릅니다.")
    answer = result["findings"]
    status = answer["status"]
    if status not in ("draft", "partial", "insufficient_evidence"):
        raise ValueError("알 수 없는 조사 결과 상태입니다.")
    by_id = {e["evidence_id"]: e for e in result.get("evidences", [])}
    mapping = {key: f"{prefix}.{key}" for key in by_id}
    cited = list(dict.fromkeys(answer.get("evidence_ids", [])))
    if not set(cited) <= by_id.keys():
        raise ValueError("인용 목록에 확보하지 않은 출처가 있습니다.")
    if status in ("draft", "partial") and not cited:
        raise ValueError("확인된 조사 결과에는 출처가 필요합니다.")
    summary = remap_citations(answer["answer"], mapping)
    numbers = []
    for raw in result.get("numbers", []):
        number = ResearchNumber.model_validate({**raw, "source_ids": [mapping[s] for s in raw["source_ids"]]})
        if number.context != context or not raw["source_ids"] or not set(raw["source_ids"]) <= set(cited):
            raise ValueError("숫자의 조회 기준 또는 인용 출처가 요청과 다릅니다.")
        numbers.append(number)
    sources = []
    for key in cited:
        evidence = by_id[key]
        metadata = evidence.get("metadata") or {}
        sources.append(EvidenceRef(id=mapping[key], source_file=evidence.get("source_file"),
            pdf_page=evidence.get("pdf_page"), source_xpath=evidence.get("source_xpath"),
            title=evidence.get("title"), chunk_id=evidence.get("chunk_id"),
            rcept_no=metadata.get("rcept_no"), account_id=metadata.get("account_id"),
            account_name=metadata.get("account_name")))
    retry = question
    components = result.get("component_status") or {}
    if (status == "partial" and components.get("explanation") == "unavailable"
            and all(components.get(k) in ("complete", "not_requested") for k in ("metrics", "ratios"))):
        document = result.get("document_research") or {}
        retry = (document.get("evaluation") or {}).get("next_query") or (result.get("plan") or {}).get("document_question") or question
    return QuestionFinding(question=question, status=status, summary=summary, numbers=numbers,
        evidence_ids=[mapping[s] for s in cited], sources=sources,
        limitations=list(answer.get("limitations", [])), component_status=components,
        unsupported_items=result.get("unsupported_items", []),
        retry_question=retry if status != "draft" else None,
        retryable=(result.get("stop_reason") not in ("needs_clarification", "invalid_denominator", "context_mismatch", "budget_exceeded", "no_new_evidence", "repeated_query", "no_next_query")
            and not (result.get("unsupported_items") and components.get("explanation") != "unavailable")),
        stop_reason=result.get("stop_reason", "unknown"),
        attempts=[{"round": round_, "question": question, "status": status,
                   "stop_reason": result.get("stop_reason"), "tool_trace": result.get("tool_trace", [])}])


def number_summary(numbers: list[ResearchNumber]) -> str:
    """이미 확인한 숫자를 다시 모델에 맡기지 않고 출처와 함께 재작성한다."""
    from fin_research.research.loop.tool_graph import LABELS
    return "\n".join(f"{LABELS.get(n.metric, n.metric)}: {Decimal(n.value):,f}{n.unit} "
                     + " ".join(f"[{s}]" for s in n.source_ids) for n in numbers)


def merge_question(previous: QuestionFinding, current: QuestionFinding) -> QuestionFinding:
    """부족한 설명만 재조사해도 기존 숫자·출처를 유지하고 성공 시 오래된 보류 안내를 제거한다."""
    merged = current.model_copy(deep=True, update={"question": previous.question,
                                                 "attempts": previous.attempts + current.attempts})
    numbers = {n.metric: n for n in previous.numbers}
    for number in current.numbers:
        old = numbers.get(number.metric)
        if old and (old.context != number.context or Decimal(old.value) != Decimal(number.value)):
            raise ValueError("재조사에서 동일 기준의 숫자가 충돌했습니다.")
        numbers[number.metric] = number
    merged.numbers = list(numbers.values())
    sources = {s.id: s for s in previous.sources}
    sources.update({s.id: s for s in current.sources})
    merged.sources = list(sources.values())
    merged.evidence_ids = list(dict.fromkeys(previous.evidence_ids + current.evidence_ids))
    if current.status == "draft":
        retained = [n for n in previous.numbers if n.metric not in {x.metric for x in current.numbers}]
        merged.summary = "\n".join(s for s in (number_summary(retained), current.summary) if s)
        merged.component_status = {**previous.component_status, **{
            k: v for k, v in current.component_status.items() if v != "not_requested"}}
    elif previous.status in ("draft", "partial"):
        merged.status = "partial"
        merged.summary = previous.summary + ("\n" + current.summary if current.status == "partial" else "")
        merged.limitations = list(dict.fromkeys(previous.limitations + current.limitations))
    merged.unsupported_items = list(dict.fromkeys(previous.unsupported_items + current.unsupported_items))
    if merged.unsupported_items:
        merged.status = "partial" if merged.numbers or merged.evidence_ids else "insufficient_evidence"
        merged.limitations += [f"미지원 요구: {item}" for item in merged.unsupported_items
                               if f"미지원 요구: {item}" not in merged.limitations]
        if current.status == "draft":
            merged.retryable = False
    return merged


def aggregate_questions(subtopic, context, records: list[QuestionFinding], round_: int) -> Finding:
    """대기·실패한 질문이 남아 있으면 일부 성공을 전체 완료로 처리하지 않는다."""
    confirmed = [r for r in records if r.status in ("draft", "partial")]
    status = "draft" if records and all(r.status == "draft" for r in records) else "partial" if confirmed else "insufficient_evidence"
    limitations = list(dict.fromkeys(note for r in records for note in r.limitations))
    for record in records:
        if record.status != "draft":
            limitations.append(f"미해결 질문: {record.question}")
    return Finding(subtopic_id=subtopic.id, status=status,
        summary="\n".join(r.summary for r in confirmed) or f"{subtopic.title}: 확보한 근거로 답할 수 없었습니다.",
        evidence_ids=list(dict.fromkeys(s for r in confirmed for s in r.evidence_ids)),
        sources=list({s.id: s for r in confirmed for s in r.sources}.values()),
        limitations=list(dict.fromkeys(limitations)), question_results=records, context=context,
        round=round_, retryable=any(r.retryable for r in records if r.status != "draft"))


def make_tool_researcher(*, search_tools=None, questions_per_subtopic=None, loop_rounds=None,
                         top_k=5, loop_fn=None, account_tool=None, model=None, source_format="xml",
                         xml_version=1, progress=None):
    """상위 Researcher에 지연 로딩·공통 예산·질문별 다중 도구 조사를 연결한다."""
    from fin_research.research.nodes.researcher import SearchToolCache
    from fin_research.research.loop.tool_graph import run_tool_research

    config = load_budget_config().get("research") or {}
    limit = questions_per_subtopic if questions_per_subtopic is not None else config.get("questions_per_subtopic", 2)
    rounds = loop_rounds if loop_rounds is not None else config.get("loop_max_iterations", 3)
    if type(limit) is not int or limit <= 0 or not 1 <= rounds <= 5 or not 1 <= top_k <= 10:
        raise ValueError("질문 수·검색 횟수·top_k 설정을 확인하세요.")
    if source_format not in ("xml", "pdf") or xml_version not in (1, 2):
        raise ValueError("문서 형식 또는 XML 버전이 올바르지 않습니다.")
    if source_format == "pdf" and xml_version != 1:
        raise ValueError("PDF 검색에는 XML v2를 지정할 수 없습니다.")
    directory = PROJECT_ROOT / "data/chroma_xml_v2" if source_format == "xml" and xml_version == 2 else PROJECT_ROOT / "data/chroma_multi"
    tools = search_tools or SearchToolCache(directory)
    run_loop = loop_fn or run_tool_research
    # 상위 라운드가 바뀌어도 질문별 검색 세션과 원문 근거를 실행 메모리에서 재사용한다.
    sessions, document_memories = {}, {}

    async def researcher(inp, config: RunnableConfig):
        """미완료 질문만 실행하고 숫자·설명·출처를 다음 Supervisor 라운드에도 유지한다."""
        subtopic, brief, round_ = inp["subtopic"], inp["brief"], inp["round"]
        previous = inp.get("previous_finding")
        try:
            context = resolve_context(brief, rcept_no=((config or {}).get("configurable") or {}).get("research_rcept_no"))
            if previous and previous.context != context:
                raise ValueError("재조사의 기업·기간·기준·공시가 이전 조사와 다릅니다.")
        except (ValueError, OSError) as exc:
            return {"findings": [Finding(subtopic_id=subtopic.id, status="insufficient_evidence", round=round_,
                        summary="조사 조건을 확인하지 못했습니다.", limitations=[str(exc)], retryable=False)]}
        records = [r.model_copy(deep=True) for r in previous.question_results] if previous else [
            QuestionFinding(question=q, retry_question=q) for q in subtopic.questions]
        pending = [i for i, r in enumerate(records) if r.status != "draft" and r.retryable]
        for index in pending[:limit]:
            old = records[index]
            question = old.retry_question or old.question
            report_progress(progress, f"[{subtopic.id} / 라운드 {round_}] 다중 도구 조사: {question}")
            try:
                budget = get_budget(config)
                if budget is not None:
                    budget.check()
                key = (((config or {}).get("configurable") or {}).get("research_session_id", id(brief)), context.model_dump_json(), subtopic.id, index)
                if key not in sessions:
                    sessions[key] = tools.session(type_context_target(context), source_format=source_format, xml_version=xml_version)
                memory = document_memories.get(key)
                result = await asyncio.to_thread(run_loop, question, context, tools.persist_directory,
                    max_rounds=rounds, top_k=top_k, budget=budget, account_tool=account_tool, model=model,
                    search_function=sessions[key],
                    source_format=source_format, xml_version=xml_version, progress=progress,
                    **({"document_memory": memory} if memory else {}))
                document = result.get("document_research")
                if document:
                    document_memories[key] = document
                current = convert_result(question, result, context, f"{subtopic.id}.r{round_}.q{index + 1}", round_)
                if document:
                    current.search_memory = {"queries": document.get("queries", []),
                        "chunk_ids": [e["chunk_id"] for e in document.get("evidences", [])],
                        "search_calls_total": sum(not h.get("skipped") for h in document.get("history", []))}
                records[index] = merge_question(old, current) if old.attempts else current
                if result.get("stop_reason") == "budget_exceeded":
                    break
            except Exception as exc:
                budget_stop = isinstance(exc, BudgetExceeded)
                message = f"{_BUDGET_MARK} {exc}" if budget_stop else f"조사 실패: {type(exc).__name__}: {exc}"
                # 기존 숫자가 있는 부분 결과는 예외가 발생해도 폐기하지 않는다.
                record = old.model_copy(deep=True)
                record.limitations = list(dict.fromkeys(record.limitations + [message]))
                record.stop_reason = "budget_exceeded" if budget_stop else "tool_error"
                record.attempts.append({"round": round_, "question": question,
                    "status": record.status, "stop_reason": record.stop_reason, "tool_trace": []})
                if budget_stop:
                    record.retryable = False
                records[index] = record
                if budget_stop:
                    break
        return {"findings": [aggregate_questions(subtopic, context, records, round_)]}
    return researcher


def type_context_target(context: FinancialContext):
    """고정된 재무 기준을 동일 기업·연도·공시의 문서 검색 대상으로 변환한다."""
    from fin_research.retrieval.search_target import SearchTarget
    return SearchTarget(context.corp_code, context.year, context.scope, context.rcept_no)
