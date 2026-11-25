"""질문 계획 → 도구 선택 → SQL·계산·문서 조사 → 공통 검증을 연결한다."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict

from fin_research.progress import report_progress
from fin_research.llm.budget import BudgetExceeded
from fin_research.research.loop.executor import ResearchToolExecutor
from fin_research.research.loop.planner import ResearchPlan, create_plan
from fin_research.research.loop.router import ToolRoute, route_plan
from fin_research.tools.financial_models import (
    CalculationResult, FinancialContext, FinancialDataError, FinancialFact,
)
from fin_research.tools.ratios import UndefinedRatioError
from fin_research.storage.sql_store import format_krw


LABELS = {
    "revenue": "매출액", "operating_profit": "영업이익", "net_income": "당기순이익",
    "total_assets": "자산총계", "total_equity": "자본총계", "total_liabilities": "부채총계",
    "current_assets": "유동자산", "current_liabilities": "유동부채", "debt_ratio": "부채비율",
    "current_ratio": "유동비율", "roe": "ROE", "operating_margin": "영업이익률",
}
from fin_research.tools.financial_catalog import CATALOG
LABELS.update({key: item["label"] for key, item in CATALOG.items()})


class ToolResearchState(TypedDict):
    """다중 도구 그래프가 누적하는 계획·실행 결과·종료 상태."""

    plan: ResearchPlan | None
    route: ToolRoute | None
    index: int
    facts: list[FinancialFact]
    calculations: list[CalculationResult]
    document_result: dict | None
    trace: list[dict]
    stop_reason: str
    limitations: list[str]


def compose_tool_result(question: str, context: FinancialContext, state: ToolResearchState) -> dict:
    """숫자는 코드로 작성하고 문서 답변의 출처 ID를 전역 ID로 재연결한다."""
    evidences, source_map = [], {}
    for fact in state["facts"]:
        identifier = f"S{len(evidences) + 1}"
        source_map[fact.id] = identifier
        evidences.append({
            "evidence_id": identifier, "chunk_id": fact.id,
            "source_file": "fin.dart_financial_accounts", "pdf_page": None,
            "text": f"{fact.context.year}년 {LABELS[fact.metric]}: {fact.value}원",
            "metadata": {**fact.context.model_dump(), "account_id": fact.account_id,
                         "account_name": fact.account_name, "source": "PostgreSQL/OpenDART fnlttSinglAcntAll",
                         "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={context.rcept_no}"},
        })
    document = state["document_result"] or {}
    document_ids = {}
    for evidence in document.get("evidences", []):
        identifier = f"S{len(evidences) + 1}"
        document_ids[evidence["evidence_id"]] = identifier
        evidences.append({**evidence, "evidence_id": identifier})
    answered = state["stop_reason"] in ("completed", "sufficient_evidence") and not (state["plan"] and state["plan"].unsupported_items)
    numbers, lines, cited = [], [], []
    plan = state["plan"]
    if plan is not None:
        for fact in state["facts"]:
            if fact.metric not in plan.metrics or fact.context.year != context.year:
                continue
            source_id = source_map[fact.id]
            numbers.append({"metric": fact.metric, "value": str(fact.value), "unit": fact.unit,
                            "context": context.model_dump(), "source_ids": [source_id]})
            lines.append(f"{LABELS[fact.metric]}: {fact.value:,f}원 ({format_krw(fact.value)}) [{source_id}]")
            cited.append(source_id)
        for calculation in state["calculations"]:
            ids = [source_map[source] for source in calculation.source_ids]
            numbers.append({"metric": calculation.metric, "value": str(calculation.value), "unit": "%",
                            "context": context.model_dump(), "source_ids": ids})
            display = format(calculation.value, ".4f").rstrip("0").rstrip(".")
            lines.append(f"{LABELS[calculation.metric]}: 약 {display}% " + " ".join(f"[{source}]" for source in ids))
            cited.extend(ids)
        findings = document.get("findings") or {}
        if findings.get("status") == "draft":
            text = re.sub(r"\[(S\d+)\]", lambda match: f"[{document_ids[match.group(1)]}]", findings["answer"])
            lines.append(text)
            cited.extend(document_ids[source] for source in findings["evidence_ids"])
    cited = list(dict.fromkeys(cited))
    partial = not answered and bool(lines)
    components = {
        "metrics": "complete" if plan and plan.metrics and all(
            any(number["metric"] == metric for number in numbers) for metric in plan.metrics
        ) else "unavailable" if plan and plan.metrics else "not_requested",
        "ratios": "complete" if plan and plan.ratios and len(state["calculations"]) == len(plan.ratios)
        else "unavailable" if plan and plan.ratios else "not_requested",
        "explanation": "complete" if (document.get("findings") or {}).get("status") == "draft"
        else "unavailable" if plan and plan.document_question else "not_requested",
    }
    if partial:
        if components["explanation"] == "unavailable":
            lines.append("설명 부분은 필요한 근거를 확보하지 못해 확정하지 못했습니다.")
        if components["ratios"] == "unavailable":
            lines.append("요청한 비율 계산은 입력 자료나 계산 조건을 확인하지 못했습니다.")
    answer = "\n".join(lines) if answered or partial else "필요한 자료나 기준을 확인하지 못해 답변을 확정할 수 없습니다."
    limitations = list(dict.fromkeys(list(state["limitations"]) + list((document.get("findings") or {}).get("limitations", []))))
    if plan:
        limitations += [f"미지원 요구: {item}" for item in plan.unsupported_items]
    if not answered and not limitations:
        limitations.append(f"조사 종료: {state['stop_reason']}")
    return {
        "question": question, "mode": "multi_tool_research", "context": context.model_dump(),
        "plan": plan.model_dump() if plan else None,
        "route": state["route"].steps if state["route"] else [],
        "stop_reason": state["stop_reason"], "tool_trace": state["trace"],
        "tool_calls": len(state["trace"]), "llm_calls": 1 + document.get("llm_calls", 0),
        "rounds": document.get("tool_calls", 0), "numbers": numbers,
        "facts": [fact.model_dump(mode="json") for fact in state["facts"]],
        "calculations": [result.model_dump(mode="json") for result in state["calculations"]],
        "evidences": evidences, "evidence_count": len(evidences),
        "document_research": document or None,
        "component_status": components,
        "unsupported_items": plan.unsupported_items if plan else [],
        "findings": {"status": "draft" if answered else "partial" if partial else "insufficient_evidence",
                     "answer": answer, "evidence_ids": cited, "limitations": limitations},
        "numeric_verification": "structured_inputs_checked" if state["facts"] else "not_performed",
        "verification": {"calculation_checked": bool(state["calculations"]),
                         "claim_against_original": "not_performed"},
    }


def run_tool_research(question: str, context: FinancialContext, persist_directory: Path, *,
                      model=None, model_name=None, account_tool=None, search_function=None,
                      budget=None, max_rounds: int = 3, top_k: int = 5, progress=None, source_format="xml", xml_version=1,
                      document_memory=None) -> dict:
    """계획·라우팅·도구 실행·검증을 LangGraph로 수행하고 기존 문서 루프는 재사용한다."""
    if not question.strip() or len(question) > 10000 or not 1 <= max_rounds <= 5 or not 1 <= top_k <= 10:
        raise ValueError("질문과 최대 검색 횟수(1~5), top_k(1~10)를 확인하세요.")
    if source_format not in ("xml", "pdf"):
        raise ValueError("source_format은 xml 또는 pdf여야 합니다.")
    if xml_version not in (1, 2) or (source_format != "xml" and xml_version != 1):
        raise ValueError("XML 버전은 1 또는 2이며 PDF에는 지정할 수 없습니다.")
    from langgraph.graph import END, START, StateGraph
    from fin_research.retrieval.search_target import SearchTarget
    from fin_research.research.loop.graph import _role_model

    planner_model = model if model is not None else _role_model("loop_planner", model_name, budget)
    target = SearchTarget(context.corp_code, context.year, context.scope, context.rcept_no)
    executor = ResearchToolExecutor(
        context, persist_directory, target, account_tool=account_tool, search_function=search_function,
        model=model, model_name=model_name, budget=budget, max_rounds=max_rounds, top_k=top_k, progress=progress,
        source_format=source_format, xml_version=xml_version,
        document_memory=document_memory,
    )

    def plan_node(state: ToolResearchState) -> dict:
        """질문을 구조화하고 허용된 도구 순서를 결정한다."""
        report_progress(progress, f"조사 계획 생성 중 · {context.company} {context.year}년 {context.scope}")
        try:
            plan = create_plan(question, context, planner_model)
            route = route_plan(plan)
        except ValueError as exc:
            return {"stop_reason": "planning_failed", "limitations": [str(exc)]}
        report_progress(progress, f"계획: {plan.reason} · 도구 순서: {' → '.join(route.steps) or '추가 확인'}")
        return {"plan": plan, "route": route,
                "stop_reason": "needs_clarification" if plan.status == "needs_clarification" else "",
                "limitations": [plan.reason] if plan.status == "needs_clarification" else []}

    def execute_node(state: ToolResearchState) -> dict:
        """다음 도구 하나를 실행하고 실패 시 후속 계산·답변 확정을 차단한다."""
        action = state["route"].steps[state["index"]]
        trace = list(state["trace"])
        report_progress(progress, f"도구 실행: {action}")
        try:
            update = {"index": state["index"] + 1}
            if action == "sql":
                facts = executor.lookup(state["route"])
                update["facts"] = facts
                update["limitations"] = list(state["limitations"]) + executor.lookup_limitations
                trace.append({"tool": "sql", "status": "ok", "detail": f"동일 공시의 금액 {len(facts)}개 확보"})
            elif action == "calc":
                update["calculations"] = executor.calculate(state["plan"].ratios, state["facts"])
                trace.append({"tool": "calc", "status": "ok", "detail": "허용된 비율과 입력 기준 검사 완료"})
            else:
                result = executor.research_documents(state["plan"].document_question)
                update["document_result"] = result
                for history in result["history"][result.get("history_start", 0):]:
                    if history.get("skipped"):
                        continue
                    trace.append({"tool": "doc_search", "status": "error" if history.get("error") else (
                        "ok" if history.get("retrieved_count", history["new_evidence_count"]) else "empty"),
                        "detail": history["query"]})
            update["trace"] = trace
            return update
        except BudgetExceeded as exc:
            # 앞 단계의 SQL 결과를 보존하면서 공통 예산 중단을 상위에 명시한다.
            trace.append({"tool": action, "status": "blocked", "detail": "공통 예산 초과"})
            return {"trace": trace, "stop_reason": "budget_exceeded", "limitations": [f"[예산 초과] {exc}"]}
        except (ValueError, RuntimeError, OSError) as exc:
            reason = exc.reason if isinstance(exc, FinancialDataError) else (
                "invalid_denominator" if isinstance(exc, UndefinedRatioError) else "tool_error")
            trace.append({"tool": action, "status": "empty" if reason == "missing_data" else (
                "blocked" if isinstance(exc, ValueError) else "error"), "detail": str(exc)})
            report_progress(progress, f"도구 실행 중단 · {reason} · {exc}")
            return {"trace": trace, "stop_reason": reason, "limitations": [str(exc)]}

    def evaluate_node(state: ToolResearchState) -> dict:
        """문서 근거 부족을 숫자 성공으로 덮어쓰지 않고 전체 답변 가능 여부를 확정한다."""
        if state["stop_reason"]:
            return {}
        document = state["document_result"]
        if document and document["findings"]["status"] != "draft":
            return {"stop_reason": document["stop_reason"], "limitations": list(state["limitations"]) + document["findings"]["limitations"]}
        if state["limitations"]:
            return {"stop_reason": "missing_data"}
        only_document = state["route"].steps == ["doc_search"]
        return {"stop_reason": document["stop_reason"] if only_document else "completed"}

    graph = StateGraph(ToolResearchState)
    graph.add_node("planner", plan_node)
    graph.add_node("executor", execute_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_edge(START, "planner")
    graph.add_conditional_edges("planner", lambda state: "evaluate" if state["stop_reason"] else "executor")
    graph.add_conditional_edges("executor", lambda state: "evaluate" if state["stop_reason"] or
                                state["index"] >= len(state["route"].steps) else "executor")
    graph.add_edge("evaluate", END)
    state = graph.compile().invoke({
        "plan": None, "route": None, "index": 0, "facts": [], "calculations": [],
        "document_result": None, "trace": [], "stop_reason": "", "limitations": [],
    }, {"recursion_limit": 12})
    report_progress(progress, f"다중 도구 조사 종료 · {state['stop_reason']}")
    return compose_tool_result(question, context, state)
