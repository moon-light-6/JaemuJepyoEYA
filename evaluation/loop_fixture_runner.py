"""API·DB 없이 기존 SQL 도구·비율 함수·문서 루프를 실행하는 평가용 어댑터.

도구 선택과 LLM 응답은 시나리오로 고정한다. 따라서 이 실행은 프로덕션의
planner/router 또는 실제 LLM·검색 품질을 검증하지 않는다. 정답 필드는 읽지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

from langchain_core.runnables import RunnableLambda

from evaluation.loop_qa import (
    FixtureSpec, LoopObservation, LoopQACase, NumericAnswer, QAContext, QAEvidence, ToolEvent,
)
from fin_research.research.loop.graph import run_research_loop
from fin_research.storage.sql_store import SQLExecutionError, SQLResult, validate_readonly_sql
from fin_research.tools.ratios import UndefinedRatioError, operating_profit_margin
from fin_research.tools.sql_tool import FinancialSQLTool


METRIC_CODES = {"revenue": "REVENUE", "operating_profit": "OPERATING_INCOME"}


class FixtureModel:
    """고정 응답만 반환하되 기존 LangChain 구조화 출력 경로를 그대로 통과시킨다."""

    model_name = "fixture-no-api"

    def __init__(self, reply: Callable):
        """스키마별 응답을 만드는 콜백을 저장한다."""
        self.reply = reply

    def with_structured_output(self, schema, **kwargs):
        """기존 도구가 구성하는 체인에 모의 응답 Runnable을 제공한다."""
        return RunnableLambda(lambda prompt: self.reply(schema))


class FixtureSQLStore:
    """모의 금액을 SQLResult로 반환하며 SQL 도구의 실제 구문 검사는 유지한다."""

    def __init__(self, fixture: FixtureSpec, context: QAContext, trace: list[ToolEvent]):
        """정답과 분리된 금액·기준·실행 이력만 전달받는다."""
        self.fixture, self.context, self.trace = fixture, context, trace

    def execute(self, sql: str) -> SQLResult:
        """허용된 SQL을 검사하고 성공·빈 결과·오류 시나리오를 재현한다."""
        normalized = validate_readonly_sql(sql)
        if self.fixture.db_error:
            self.trace.append(ToolEvent(tool="sql", status="error", detail="평가용 DB 오류"))
            raise SQLExecutionError("평가용 DB 조회 실패")
        rows = [{
            "std_account_code": METRIC_CODES[metric], "amount": amount,
            "period_end": date(self.fixture.fact_years.get(metric, self.context.year), 12, 31),
            "pdf_page": 1, "chunk_id": f"sql:{metric}",
        } for metric, amount in self.fixture.amounts.items()]
        self.trace.append(ToolEvent(tool="sql", status="ok" if rows else "empty",
                                    detail=f"모의 재무 값 {len(rows)}개 조회"))
        return SQLResult(normalized, ["std_account_code", "amount", "period_end", "pdf_page", "chunk_id"],
                         rows, len(rows), False)


def query_fixture_sql(question: str, fixture: FixtureSpec, context: QAContext,
                      trace: list[ToolEvent]) -> tuple[list[NumericAnswer], list[QAEvidence]]:
    """기존 FinancialSQLTool을 모의 모델·DB와 연결해 실제 입력·출력 경로를 실행한다."""
    sql = (
        "SELECT std_account_code, amount, period_end, pdf_page, chunk_id "
        "FROM fin.v_fin_value "
        f"WHERE corp_code = '{context.corp_code}' AND fs_scope = '{context.scope}' "
        f"AND period_end = DATE '{context.year}-12-31' AND value_status = 'ok' "
        "AND std_account_code IN ('REVENUE', 'OPERATING_INCOME')"
    )
    model = FixtureModel(lambda schema: {"status": "ready", "sql": sql, "reason": "평가용 고정 SQL"})
    result = FinancialSQLTool(model, FixtureSQLStore(fixture, context, trace)).run(question)
    numbers, evidences = [], []
    code_to_metric = {code: metric for metric, code in METRIC_CODES.items()}
    for row in result["result"]["rows"]:
        metric = code_to_metric[row["std_account_code"]]
        source_id = row["chunk_id"]
        fact_context = context.model_copy(update={"year": row["period_end"].year})
        numbers.append(NumericAnswer(metric=metric, value=row["amount"], unit="원",
                                     context=fact_context, source_ids=[source_id]))
        evidences.append(QAEvidence(id=source_id, text=f"{metric}={row['amount']}원",
                                     source_file="synthetic_financial_fixture", context=fact_context))
    return numbers, evidences


class FixtureSearch:
    """검색 결과를 회차별로 공급하고 실제 루프에서 확보한 문서 순서를 추적한다."""

    def __init__(self, fixture: FixtureSpec, trace: list[ToolEvent]):
        """검색 자료와 모의 평가 응답의 상태를 초기화한다."""
        self.fixture, self.trace = fixture, trace
        self.documents = {document.id: document for document in fixture.documents}
        self.seen = []
        self.calls = 0
        self.decisions = iter(fixture.decisions)
        self.selected = []

    def search(self, question: str, top_k: int) -> list[dict]:
        """현재 회차의 문서를 반환하고 실제 검색 호출을 기록한다."""
        batches = self.fixture.retrieval_rounds
        ids = batches[min(self.calls, len(batches) - 1)] if batches else []
        self.calls += 1
        self.trace.append(ToolEvent(tool="doc_search", status="ok" if ids else "empty", detail=question))
        hits = []
        for source_id in ids[:top_k]:
            if source_id not in self.seen:
                self.seen.append(source_id)
            hits.append({"chunk_id": source_id, "text": self.documents[source_id].text,
                         "source_file": "synthetic_report.txt", "pdf_page": 1, "metadata": {}})
        return hits

    def reply(self, schema) -> dict:
        """평가와 답변의 모의 응답을 만들며 정답 숫자나 기대 판정은 참조하지 않는다."""
        if schema.__name__ == "EvidenceEvaluation":
            decision = next(self.decisions)
            if any(source_id not in self.seen for source_id in decision.source_ids):
                raise ValueError("검색되기 전에 평가 응답이 출처를 선택했습니다.")
            self.selected = decision.source_ids
            return {"sufficient": decision.sufficient, "reason": decision.reason,
                    "evidence_ids": [f"S{self.seen.index(source) + 1}" for source in self.selected],
                    "missing_items": [] if decision.sufficient else [decision.reason],
                    "next_query": decision.next_query}
        if schema.__name__ == "GroundedAnswer":
            ids = [f"S{self.seen.index(source) + 1}" for source in self.selected]
            return {"status": "draft", "answer": " ".join(
                f"{self.documents[source].text} [{identifier}]" for source, identifier in zip(self.selected, ids)
            ), "evidence_ids": ids, "limitations": []}
        raise ValueError(f"지원하지 않는 모의 응답 스키마: {schema.__name__}")


def run_document_fixture(question: str, fixture: FixtureSpec, context: QAContext,
                          trace: list[ToolEvent]) -> LoopObservation:
    """실제 LangGraph 재검색·출처 검사·종료 제어를 모의 검색·LLM으로 실행한다."""
    search = FixtureSearch(fixture, trace)
    result = run_research_loop(
        question, Path("unused-fixture-store"), max_rounds=fixture.max_rounds,
        model=FixtureModel(search.reply), search_function=search.search,
    )
    findings = result["findings"]
    id_map = {evidence["evidence_id"]: evidence["chunk_id"] for evidence in result["evidences"]}
    return LoopObservation(
        case_id="pending", status="answered" if findings["status"] == "draft" else "abstained",
        answer=findings["answer"],
        evidences=[QAEvidence(id=evidence["chunk_id"], text=evidence["text"],
                             source_file=evidence["source_file"], context=context)
                   for evidence in result["evidences"]],
        cited_source_ids=[id_map[identifier] for identifier in findings["evidence_ids"]],
        tool_trace=trace, stop_reason=result["stop_reason"], rounds=result["tool_calls"],
        limitations=findings["limitations"],
    )


def run_fixture_case(case: LoopQACase) -> LoopObservation:
    """평가용 도구 조합을 실행한다. 생산 planner/router의 자동 선택을 대신 구현하지 않는다."""
    fixture, context = case.fixture, case.context
    if fixture is None:
        raise ValueError(f"{case.id}: 모의 실행 입력이 없습니다.")
    trace = []
    if fixture.task in ("document", "control"):
        return run_document_fixture(case.question, fixture, context, trace).model_copy(update={"case_id": case.id})
    observation = LoopObservation(case_id=case.id, status="abstained", answer="",
                                  stop_reason="missing_data", tool_trace=[])
    try:
        numbers, evidences = query_fixture_sql(case.question, fixture, context, trace)
    except SQLExecutionError as exc:
        return observation.model_copy(update={"stop_reason": "tool_error", "tool_trace": trace,
                                               "limitations": [str(exc)]})
    observation.evidences = evidences
    observation.tool_trace = trace
    if not numbers:
        observation.limitations = ["필요한 재무 데이터가 없습니다. 0으로 대체하지 않습니다."]
        return observation
    if any(number.context != context for number in numbers):
        observation.stop_reason = "context_mismatch"
        observation.limitations = ["조회한 숫자의 회사·기간·기준이 질문과 다릅니다."]
        return observation
    if fixture.task == "margin":
        inputs = {number.metric: number for number in numbers}
        if not {"revenue", "operating_profit"} <= inputs.keys():
            observation.limitations = ["영업이익률 계산에 필요한 매출액 또는 영업이익이 없습니다."]
            return observation
        try:
            value = operating_profit_margin(inputs["operating_profit"].value, inputs["revenue"].value)
        except UndefinedRatioError:
            trace.append(ToolEvent(tool="calc", status="blocked", detail="분모가 0 이하"))
            observation.tool_trace = trace
            observation.stop_reason = "invalid_denominator"
            observation.limitations = ["매출액이 0 이하라 영업이익률을 확정할 수 없습니다."]
            return observation
        trace.append(ToolEvent(tool="calc", status="ok", detail="ratios.operating_profit_margin 실행"))
        numbers = [NumericAnswer(metric="operating_margin", value=value, unit="%", context=context,
                                 source_ids=[source.id for source in evidences])]
    if fixture.task == "mixed":
        document_result = run_document_fixture(case.question, fixture, context, trace)
        observation.evidences += document_result.evidences
        observation.rounds = document_result.rounds
        observation.tool_trace = trace
        if document_result.status != "answered":
            observation.stop_reason = document_result.stop_reason
            observation.limitations = document_result.limitations
            return observation
        observation.answer = document_result.answer + " "
        observation.cited_source_ids = document_result.cited_source_ids
    observation.status = "answered"
    observation.numbers = numbers
    observation.cited_source_ids += [source for number in numbers for source in number.source_ids]
    observation.answer += "; ".join(f"{number.metric}: {number.value}{number.unit}" for number in numbers)
    observation.tool_trace = trace
    observation.stop_reason = "completed"
    return observation
