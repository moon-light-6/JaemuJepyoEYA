"""선택한 기업·공시를 대상으로 문서 검색 도구를 실행한다."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fin_research.tools.doc_search_tool import search_documents

if TYPE_CHECKING:
    from fin_research.retrieval.search_target import SearchTarget


def execute_search(
    question: str, persist_directory: Path, top_k: int, target: SearchTarget | None = None,
) -> list[dict]:
    """기존 문서 전용 루프의 검색 호출과 대상 지정 방식을 유지한다."""
    return search_documents(question, persist_directory, top_k, target)


class ResearchToolExecutor:
    """계획에서 허용된 SQL·계산·문서 조사만 실행한다."""

    def __init__(self, context, persist_directory, target, *, account_tool=None, search_function=None,
                 model=None, model_name=None, budget=None, max_rounds=3, top_k=5, progress=None, source_format="xml", xml_version=1,
                 document_memory=None):
        """도구와 모델을 주입하고 실제 DB·임베딩은 필요한 단계에서만 준비한다."""
        self.context, self.persist_directory, self.target = context, persist_directory, target
        self.account_tool, self.search_function = account_tool, search_function
        self.model, self.model_name, self.budget = model, model_name, budget
        self.max_rounds, self.top_k, self.progress = max_rounds, top_k, progress
        self.source_format = source_format
        self.xml_version = xml_version
        self.document_memory = document_memory
        self.lookup_limitations = []
        self.calculation_limitations = []
        self.calculation_stop_reason = ""

    def lookup(self, route):
        """필요 지표를 조회한 뒤 회사·기간·공시가 맞고 입력이 모두 있는지 확인한다."""
        from fin_research.storage.sql_store import PostgresReadOnlyStore
        from fin_research.tools.financial_account_tool import FinancialAccountTool
        from fin_research.tools.financial_models import FinancialDataError

        if self.account_tool is None:
            self.account_tool = FinancialAccountTool(PostgresReadOnlyStore(max_rows=1000))
        facts = self.account_tool.lookup(self.context, route.lookup_metrics,
                                         include_prior_equity=route.include_prior_equity)
        for fact in facts:
            allowed_years = [self.context.year]
            if route.include_prior_equity and fact.metric == "total_equity":
                allowed_years.append(self.context.year - 1)
            if fact.context.year not in allowed_years or fact.context != self.context.model_copy(update={"year": fact.context.year}):
                raise FinancialDataError("조회 숫자의 회사·기간·기준·공시가 다릅니다.", "context_mismatch")
        for metric in route.lookup_metrics:
            matches = [fact for fact in facts if fact.metric == metric and fact.context.year == self.context.year]
            if len(matches) != 1:
                if len(matches) > 1:
                    raise FinancialDataError(f"{self.context.year}년 {metric}이 중복됩니다.", "ambiguous_data")
                self.lookup_limitations.append(f"{self.context.year}년 {metric} 자료가 없습니다.")
        if not facts:
            raise FinancialDataError("요청한 지표의 자료가 없습니다.")
        return facts

    def calculate(self, names, facts):
        """코드에 정의된 재무비율만 계산하고 입력 출처를 유지한다."""
        from fin_research.tools.calc_tool import calculate_ratio
        from fin_research.tools.financial_models import FinancialDataError
        from fin_research.tools.ratios import UndefinedRatioError
        results = []
        for name in names:
            try:
                results.append(calculate_ratio(name, facts, self.context))
            except (FinancialDataError, UndefinedRatioError) as exc:
                if isinstance(exc, FinancialDataError) and exc.reason != "missing_data":
                    raise
                self.calculation_limitations.append(f"{name}: {exc}")
                self.calculation_stop_reason = "invalid_denominator" if isinstance(exc, UndefinedRatioError) else "missing_data"
        return results

    def research_documents(self, question):
        """기존 LangGraph 재검색 루프를 설명 질문에 연결한다."""
        from fin_research.research.loop.graph import run_research_loop
        from fin_research.tools.doc_search_tool import DocumentSearchTool, XMLDocumentSearchTool

        if self.search_function is None:
            tool_type = XMLDocumentSearchTool if self.source_format == "xml" else DocumentSearchTool
            options = {"chunk_version": self.xml_version} if self.source_format == "xml" else {}
            self.search_function = tool_type(self.persist_directory, self.target, **options).search
        return run_research_loop(
            question, self.persist_directory, self.top_k, self.model_name, self.max_rounds,
            model=self.model, search_function=self.search_function, target=self.target,
            budget=self.budget, progress=self.progress,
            previous_result=self.document_memory,
        )
