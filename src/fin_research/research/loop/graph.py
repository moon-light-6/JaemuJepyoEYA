"""First pass: document search followed by optional grounded GPT generation.

Planner/Router/Evaluator and repeated searches will be added in the next step.
"""

from pathlib import Path

from fin_research.research.loop.executor import execute_search
from fin_research.research.loop.findings import generate_findings


def run_once(
    question: str,
    persist_directory: Path,
    top_k: int = 5,
    model_name: str = "small",
    search_only: bool = False,
) -> dict:
    if not question.strip():
        raise ValueError("질문을 입력하세요.")

    model = None
    if not search_only:
        from fin_research.llm.client import get_chat_model

        # Fail on missing credentials before loading the local embedding model.
        model = get_chat_model(model_name)

    evidences = execute_search(question.strip(), persist_directory, top_k)
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
    }
