"""1회 검색 흐름과 근거 평가·재검색을 수행하는 LangGraph를 제공합니다."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING

from fin_research.research.loop.evaluator import evaluate_evidence
from fin_research.research.loop.executor import execute_search
from fin_research.research.loop.findings import GroundedAnswer, generate_findings
from fin_research.research.loop.state import LoopState
from fin_research.tools.doc_search_tool import DocumentSearchTool

if TYPE_CHECKING:
    from fin_research.llm.budget import Budget
    from fin_research.retrieval.search_target import SearchTarget


def _role_model(role: str, model_name: str | None, budget: Budget | None):
    """models.yaml roles 기준으로 LLM 생성. model_name을 주면 이번 실행만 large/small 지정.

    budget을 주면 호출 수·토큰이 오케스트레이션 Budget에 집계되고 상한 초과 시 중단된다.
    """
    from fin_research.llm.router import get_llm_for_role

    return get_llm_for_role(role, budget=budget, model_key=model_name)


def run_once(
    question: str,
    persist_directory: Path,
    top_k: int = 5,
    model_name: str | None = None,
    search_only: bool = False,
    *,
    budget: Budget | None = None,
    target: SearchTarget | None = None,
) -> dict:
    """검색 1회 + (선택) 답변 초안. target을 주면 그 기업·공시의 컬렉션을 검색한다."""
    if not question.strip():
        raise ValueError("질문을 입력하세요.")

    model = None
    if not search_only:
        # 로컬 임베딩 모델을 불러오기 전에 API 키를 확인하고, 누락 시 즉시 중단합니다.
        model = _role_model("loop_findings", model_name, budget)

    evidences = execute_search(question.strip(), persist_directory, top_k, target)
    findings = None
    if not search_only:
        findings = generate_findings(question, evidences, model).model_dump()

    return {
        "question": question.strip(),
        "mode": "search_only" if search_only else "search_and_draft",
        "model": getattr(model, "model_name", None),
        "stop_reason": "single_pass",
        "tool_calls": 1,
        "evidence_count": len(evidences),
        "evidences": evidences,
        "findings": findings,
        "numeric_verification": "not_performed",
        "collection": target.collection_name if target else None,
    }


def run_research_loop(
    question: str,
    persist_directory: Path,
    top_k: int = 5,
    model_name: str | None = None,
    max_rounds: int = 3,
    *,
    model=None,
    search_function: Callable[[str, int], list[dict]] | None = None,
    budget: Budget | None = None,
    target: SearchTarget | None = None,
) -> dict:
    """검색·평가를 제한 횟수만큼 반복하고 근거와 조사 이력을 반환합니다.

    model을 주면(테스트) 평가·작성 모두 그 모델을 쓰고, 없으면 models.yaml의
    loop_evaluator·loop_findings 역할로 각각 생성한다.
    target(SearchTarget)을 주면 그 기업·공시의 Chroma 컬렉션을 검색한다.
    """
    if not question.strip():
        raise ValueError("질문을 입력하세요.")
    if not 1 <= max_rounds <= 5:
        raise ValueError("max_rounds는 1~5 사이여야 합니다.")
    if not 1 <= top_k <= 10:
        raise ValueError("top_k는 1~10 사이여야 합니다.")
    if model is not None:
        evaluator_model = findings_model = model
    else:
        evaluator_model = _role_model("loop_evaluator", model_name, budget)
        findings_model = _role_model("loop_findings", model_name, budget)
    if search_function is None:
        search_function = DocumentSearchTool(persist_directory, target).search

    from langgraph.graph import END, START, StateGraph

    def search_node(state: LoopState) -> dict:
        query = state["query"]
        history = {
            "round": state["rounds"] + 1, "query": query,
            "new_evidence_count": 0, "evaluation": None,
        }
        try:
            hits = search_function(query, top_k)
        except (OSError, ValueError, RuntimeError) as exc:
            history["error"] = str(exc)
            return {
                "rounds": state["rounds"] + 1,
                "queries": state["queries"] + [query],
                "history": state["history"] + [history],
                "stop_reason": "tool_error",
            }

        # 같은 청크는 누적하지 않고, 조사 전체에서 출처 ID를 고정합니다.
        evidences = list(state["evidences"])
        known = {(item["source_file"], item["chunk_id"]) for item in evidences}
        for hit in hits:
            key = (hit["source_file"], hit["chunk_id"])
            if key not in known:
                evidences.append({**hit, "evidence_id": f"S{len(evidences) + 1}"})
                known.add(key)
                history["new_evidence_count"] += 1
        no_new_evidence = state["rounds"] > 0 and history["new_evidence_count"] == 0
        return {
            "evidences": evidences,
            "rounds": state["rounds"] + 1,
            "queries": state["queries"] + [query],
            "history": state["history"] + [history],
            "stop_reason": "no_new_evidence" if no_new_evidence else "",
        }

    def evaluate_node(state: LoopState) -> dict:
        if state["stop_reason"]:
            return {}
        evaluation = evaluate_evidence(
            state["question"], state["evidences"], state["queries"], evaluator_model,
        )
        next_query = (evaluation.next_query or "").strip()
        normalized_queries = {" ".join(q.split()).casefold() for q in state["queries"]}
        if evaluation.sufficient:
            stop_reason = "sufficient_evidence"
        elif state["rounds"] >= max_rounds:
            stop_reason = "max_rounds"
        elif not next_query:
            stop_reason = "no_next_query"
        elif " ".join(next_query.split()).casefold() in normalized_queries:
            stop_reason = "repeated_query"
        else:
            stop_reason = ""

        history = state["history"][:-1] + [
            {**state["history"][-1], "evaluation": evaluation.model_dump()}
        ]
        return {
            "evaluation": evaluation.model_dump(), "history": history,
            "query": next_query, "stop_reason": stop_reason,
            "llm_calls": state["llm_calls"] + 1,
        }

    def findings_node(state: LoopState) -> dict:
        evaluation = state["evaluation"] or {}
        if state["stop_reason"] == "sufficient_evidence":
            selected_ids = set(evaluation["evidence_ids"])
            selected = [item for item in state["evidences"] if item["evidence_id"] in selected_ids]
            errors = []
            generation_question = state["question"]
            for attempt in range(2):
                try:
                    findings = generate_findings(generation_question, selected, findings_model)
                    return {
                        "findings": findings.model_dump(),
                        "llm_calls": state["llm_calls"] + attempt + 1,
                        "generation_errors": errors,
                        "stop_reason": (
                            "answer_insufficient" if findings.status == "insufficient_evidence"
                            else state["stop_reason"]
                        ),
                    }
                except ValueError as exc:
                    errors.append(str(exc))
                    generation_question = (
                        state["question"]
                        + "\n\n이전 답변의 출력 검사가 실패했습니다. "
                        + str(exc)
                        + " 답변에 [S1] 같은 개별 출처를 표기하고 evidence_ids와 정확히 맞춰 다시 작성하세요."
                    )
            findings = GroundedAnswer(
                status="insufficient_evidence",
                answer="답변의 출처 또는 출력 형식을 확인하지 못해 초안을 확정할 수 없습니다.",
                evidence_ids=[], limitations=errors,
            )
            return {
                "findings": findings.model_dump(),
                "llm_calls": state["llm_calls"] + 2,
                "generation_errors": errors,
                "stop_reason": "generation_validation_failed",
            }

        # 평가에서 부족한 근거를 답변 생성 단계가 임의로 충분하다고 바꾸지 못하게 합니다.
        limitations = list(evaluation.get("missing_items", []))
        if state["stop_reason"] == "tool_error":
            limitations.append("문서 검색 도구 실행에 실패했습니다. history의 오류를 확인하세요.")
        limitations.append(f"조사 종료 이유: {state['stop_reason']}")
        findings = GroundedAnswer(
            status="insufficient_evidence",
            answer="확보한 근거만으로는 질문에 대한 답변을 확정할 수 없습니다.",
            evidence_ids=[], limitations=limitations,
        )
        return {"findings": findings.model_dump()}

    graph = StateGraph(LoopState)
    graph.add_node("search", search_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("findings", findings_node)
    graph.add_edge(START, "search")
    graph.add_edge("search", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        lambda state: "finish" if state["stop_reason"] else "retry",
        {"finish": "findings", "retry": "search"},
    )
    graph.add_edge("findings", END)
    initial_state: LoopState = {
        "question": question.strip(), "query": question.strip(),
        "queries": [], "evidences": [], "history": [],
        "evaluation": None, "findings": None,
        "rounds": 0, "llm_calls": 0, "stop_reason": "",
        "generation_errors": [],
    }
    state = graph.compile().invoke(initial_state, {"recursion_limit": 2 * max_rounds + 5})
    return {
        "question": state["question"], "mode": "iterative_research",
        "model": getattr(findings_model, "model_name", None),
        "evaluator_model": getattr(evaluator_model, "model_name", None),
        "stop_reason": state["stop_reason"], "tool_calls": state["rounds"],
        "max_rounds": max_rounds, "llm_calls": state["llm_calls"],
        "evidence_count": len(state["evidences"]), "evidences": state["evidences"],
        "evaluation": state["evaluation"], "history": state["history"],
        "generation_errors": state["generation_errors"],
        "findings": state["findings"], "numeric_verification": "not_performed",
        "collection": target.collection_name if target else None,
    }
