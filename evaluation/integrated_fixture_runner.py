"""새 생산용 도구 그래프를 기존 12개 사례의 모의 입력으로 통합 평가한다."""

from pathlib import Path

from evaluation.loop_fixture_runner import FixtureModel, FixtureSearch
from evaluation.loop_qa import LoopObservation, LoopQACase, NumericAnswer, QAContext, QAEvidence, ToolEvent
from fin_research.research.loop.tool_graph import run_tool_research
from fin_research.storage.sql_store import SQLExecutionError
from fin_research.tools.financial_models import FinancialContext, FinancialFact


class IntegrationAccountTool:
    """실제 DB만 대체하고 생산 executor와 계산 전 검사를 그대로 실행한다."""

    def __init__(self, fixture):
        """정답이 아닌 시나리오의 입력 금액만 저장한다."""
        self.fixture = fixture

    def lookup(self, context, metrics, *, include_prior_equity=False):
        """요청 지표의 모의 금액·누락·연도 불일치·DB 오류를 재현한다."""
        if self.fixture.db_error:
            raise SQLExecutionError("평가용 DB 조회 실패")
        return [FinancialFact(
            id=f"sql:{metric}", metric=metric, value=value,
            context=context.model_copy(update={"year": self.fixture.fact_years.get(metric, context.year)}),
        ) for metric, value in self.fixture.amounts.items() if metric in metrics]


def observation_from_tool_result(case_id: str, result: dict) -> LoopObservation:
    """생산 루프 결과를 loop_qa 관측 형식으로 변환하고 원래 출처 ID를 복원한다."""
    base = result["context"]
    context = QAContext.model_validate({key: base[key] for key in ("company", "corp_code", "year", "scope", "currency")})
    id_map = {evidence["evidence_id"]: evidence["chunk_id"] for evidence in result["evidences"]}
    evidences = []
    for evidence in result["evidences"]:
        metadata = evidence.get("metadata", {})
        source_context = context.model_copy(update={
            "company": metadata.get("company", context.company),
            "corp_code": metadata.get("corp_code", context.corp_code),
            "year": metadata.get("year", metadata.get("fiscal_year", context.year)),
            "scope": metadata.get("scope", metadata.get("statement_scope", context.scope)),
            "currency": metadata.get("currency", context.currency),
        })
        evidences.append(QAEvidence(id=evidence["chunk_id"], text=evidence["text"],
                                     source_file=evidence["source_file"], context=source_context))
    return LoopObservation(
        case_id=case_id, status={"draft": "answered", "partial": "partial"}.get(result["findings"]["status"], "abstained"),
        component_status=result.get("component_status", {}),
        answer=result["findings"]["answer"],
        numbers=[NumericAnswer(metric=number["metric"], value=number["value"], unit=number["unit"],
                               context=QAContext.model_validate({key: number["context"][key]
                                   for key in ("company", "corp_code", "year", "scope", "currency")}),
                               source_ids=[id_map[source] for source in number["source_ids"]])
                 for number in result["numbers"]],
        evidences=evidences, cited_source_ids=[id_map[source] for source in result["findings"]["evidence_ids"]],
        tool_trace=[ToolEvent.model_validate(event) for event in result["tool_trace"]],
        stop_reason=result["stop_reason"], rounds=result["rounds"], limitations=result["findings"]["limitations"],
    )


def run_integrated_fixture(case: LoopQACase) -> LoopObservation:
    """계획 모델과 외부 도구의 입출력만 고정하고 실제 planner·router·executor·그래프를 실행한다."""
    fixture = case.fixture
    if fixture is None:
        raise ValueError("통합 모의 실행 입력이 없습니다.")
    context = FinancialContext(**case.context.model_dump(), rcept_no="20260301000001")
    search = FixtureSearch(fixture, [])

    def reply(schema):
        """계획은 모의 작업 요구로 만들고 문서 평가·답변은 기존 대역에 위임한다."""
        if schema.__name__ == "ResearchPlan":
            return {"status": "ready", "metrics": ["revenue"] if fixture.task in ("numeric", "missing", "mixed") else [],
                    "ratios": ["operating_margin"] if fixture.task == "margin" else [],
                    "document_question": case.question if fixture.task in ("document", "control", "mixed") else None,
                    "reason": "통합 모의 질문 요구"}
        return search.reply(schema)

    result = run_tool_research(
        case.question, context, Path("unused-fixture-store"), model=FixtureModel(reply),
        account_tool=IntegrationAccountTool(fixture), search_function=search.search, max_rounds=fixture.max_rounds,
    )
    return observation_from_tool_result(case.id, result)
