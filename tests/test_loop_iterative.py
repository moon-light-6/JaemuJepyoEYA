"""모의 검색과 모델로 재검색·종료·출처 보존을 검증합니다."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fin_research.research.loop.evaluator import evaluate_evidence
from fin_research.research.loop.graph import run_research_loop
from fin_research.tools.doc_search_tool import DocumentSearchTool


def evidence(number):
    return {
        "evidence_id": "S1", "chunk_id": f"chunk_{number}",
        "text": f"검색 근거 {number}", "source_file": "example.pdf",
        "pdf_page": number, "metadata": {},
    }


def evaluation(sufficient=False, next_query="추가 검색", ids=None):
    return {
        "sufficient": sufficient, "reason": "테스트용 근거 평가",
        "evidence_ids": ids or [],
        "missing_items": [] if sufficient else ["별도 근거 필요"],
        "next_query": None if sufficient else next_query,
    }


def model_responses(*responses):
    """실제 LangChain 연결을 사용하되 모델 응답만 고정합니다."""
    queue = iter(responses)
    model = Mock()
    model.model_name = "test-model"
    model.with_structured_output.side_effect = lambda *a, **kw: RunnableLambda(
        lambda prompt: next(queue)
    )
    return model


def answer(source="S1", status="draft"):
    return {
        "status": status, "answer": f"근거를 확인했습니다. [{source}]",
        "evidence_ids": [source], "limitations": [],
    }


class IterativeLoopTests(unittest.TestCase):
    def test_progress_reports_real_retry_without_changing_result(self):
        def run(progress=None):
            return run_research_loop(
                "원래 질문", Path("unused"), max_rounds=3,
                model=model_responses(evaluation(), evaluation(True, ids=["S2"]), answer("S2")),
                search_function=Mock(side_effect=[[evidence(1)], [evidence(2)]]),
                progress=progress,
            )
        messages = []
        actual = run(messages.append)
        self.assertEqual(actual, run())
        transcript = "\n".join(messages)
        self.assertIn("[조사 1/3] 문서 검색 시작", transcript)
        self.assertIn("재검색 결정 · 다음 검색어: 추가 검색", transcript)
        self.assertIn("[조사 2/3] 문서 검색 시작", transcript)
        self.assertIn("새 근거 S2", transcript)
        self.assertIn("평가 결과: 충분", transcript)
        self.assertIn("검색 2회", messages[-1])

    def run_loop(self, model, search, max_rounds=3):
        return run_research_loop(
            "원래 질문", Path("unused"), max_rounds=max_rounds,
            model=model, search_function=search,
        )

    def test_sufficient_first_search(self):
        model = model_responses(evaluation(True, ids=["S1"]), answer())
        search = Mock(return_value=[evidence(1)])
        result = self.run_loop(model, search)
        self.assertEqual(result["stop_reason"], "sufficient_evidence")
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(result["llm_calls"], 2)
        self.assertEqual(result["findings"]["status"], "draft")

    def test_rewrite_accumulates_sources_and_keeps_ids(self):
        model = model_responses(
            evaluation(), evaluation(True, ids=["S2"]), answer("S2"),
        )
        search = Mock(side_effect=[[evidence(1)], [evidence(1), evidence(2)]])
        result = self.run_loop(model, search)
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(result["evidence_count"], 2)
        self.assertEqual([item["evidence_id"] for item in result["evidences"]], ["S1", "S2"])
        self.assertEqual(result["findings"]["evidence_ids"], ["S2"])
        self.assertEqual(result["history"][1]["query"], "추가 검색")
        self.assertEqual(result["history"][1]["new_evidence_count"], 1)

    def test_repeated_query_stops_before_search(self):
        model = model_responses(evaluation(next_query="  원래   질문  "))
        search = Mock(return_value=[evidence(1)])
        result = self.run_loop(model, search)
        self.assertEqual(result["stop_reason"], "repeated_query")
        self.assertEqual(search.call_count, 1)
        self.assertEqual(result["findings"]["status"], "insufficient_evidence")

    def test_round_limit_prevents_extra_search(self):
        model = model_responses(evaluation())
        result = self.run_loop(model, Mock(return_value=[evidence(1)]), max_rounds=1)
        self.assertEqual(result["stop_reason"], "max_rounds")
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(result["llm_calls"], 1)

    def test_no_new_evidence_stops_without_another_model_call(self):
        model = model_responses(evaluation())
        result = self.run_loop(model, Mock(return_value=[evidence(1)]))
        self.assertEqual(result["stop_reason"], "no_new_evidence")
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(result["llm_calls"], 1)
        self.assertIsNone(result["history"][1]["evaluation"])

    def test_search_error_is_preserved(self):
        model = model_responses()
        result = self.run_loop(model, Mock(side_effect=RuntimeError("테스트 오류")))
        self.assertEqual(result["stop_reason"], "tool_error")
        self.assertEqual(result["history"][0]["error"], "테스트 오류")
        self.assertEqual(result["llm_calls"], 0)

    def test_no_next_query_returns_insufficient(self):
        model = model_responses(evaluation(next_query=None))
        result = self.run_loop(model, Mock(return_value=[evidence(1)]))
        self.assertEqual(result["stop_reason"], "no_next_query")
        self.assertEqual(result["findings"]["status"], "insufficient_evidence")

    def test_empty_first_search_can_retry(self):
        model = model_responses(evaluation(), evaluation(True, ids=["S1"]), answer())
        search = Mock(side_effect=[[], [evidence(1)]])
        result = self.run_loop(model, search)
        self.assertEqual(result["stop_reason"], "sufficient_evidence")
        self.assertEqual(result["tool_calls"], 2)

    def test_writer_can_report_insufficient_after_evaluation(self):
        model = model_responses(
            evaluation(True, ids=["S1"]), answer(status="insufficient_evidence"),
        )
        result = self.run_loop(model, Mock(return_value=[evidence(1)]))
        self.assertEqual(result["stop_reason"], "answer_insufficient")

    def test_invalid_writer_citation_is_repaired_once(self):
        model = model_responses(
            evaluation(True, ids=["S1"]), answer("S99"), answer(),
        )
        result = self.run_loop(model, Mock(return_value=[evidence(1)]))
        self.assertEqual(result["stop_reason"], "sufficient_evidence")
        self.assertEqual(result["llm_calls"], 3)
        self.assertEqual(len(result["generation_errors"]), 1)

    def test_repeated_invalid_citation_returns_safe_result(self):
        model = model_responses(
            evaluation(True, ids=["S1"]), answer("S99"), answer("S99"),
        )
        result = self.run_loop(model, Mock(return_value=[evidence(1)]))
        self.assertEqual(result["stop_reason"], "generation_validation_failed")
        self.assertEqual(result["findings"]["status"], "insufficient_evidence")
        self.assertEqual(result["llm_calls"], 3)

    def test_evaluator_rejects_unknown_source(self):
        model = model_responses(evaluation(True, ids=["S99"]))
        with self.assertRaisesRegex(ValueError, "검색되지 않은"):
            evaluate_evidence("질문", [{**evidence(1), "evidence_id": "S1"}], [], model)

    def test_evaluator_rejects_sufficient_without_source(self):
        model = model_responses(evaluation(True))
        with self.assertRaisesRegex(ValueError, "유효한 출처"):
            evaluate_evidence("질문", [], [], model)

    def test_invalid_budget_fails_before_model_call(self):
        with self.assertRaisesRegex(ValueError, "max_rounds"):
            self.run_loop(model_responses(), Mock(), max_rounds=0)

    def test_embedding_store_is_loaded_once_for_multiple_searches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chroma.sqlite3").touch()
            with patch("fin_research.retrieval.chroma_retriever.create_chroma") as create:
                with patch("fin_research.retrieval.chroma_retriever.search_chroma", return_value=[]):
                    tool = DocumentSearchTool(root)
                    tool.search("첫 질문")
                    tool.search("두 번째 질문")
                    create.assert_called_once_with(root)

    def test_search_target_selects_company_collection(self):
        from fin_research.retrieval.search_target import SearchTarget

        target = SearchTarget("00126380", 2025, "consolidated", "20260310002820")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chroma.sqlite3").touch()
            with patch("fin_research.retrieval.chroma_retriever.create_chroma") as create:
                with patch(
                    "fin_research.retrieval.chroma_retriever.search_chroma",
                    return_value=[{"chunk_id": "c1"}, {"chunk_id": "c2"}],
                ) as search:
                    tool = DocumentSearchTool(root, target)
                    hits = tool.search("매출액", 2)
            create.assert_called_once_with(root, collection_name=target.collection_name)
            self.assertEqual(search.call_args.kwargs["collection_name"], target.collection_name)
            self.assertEqual([h["evidence_id"] for h in hits], ["S1", "S2"])

    def test_loop_passes_search_target_to_search_tool(self):
        from fin_research.retrieval.search_target import SearchTarget

        target = SearchTarget("00126380", 2025, "consolidated", "20260310002820")
        with patch("fin_research.research.loop.graph.DocumentSearchTool") as tool_cls:
            tool_cls.return_value.search.return_value = []
            model = model_responses(evaluation(False, next_query=None))
            result = run_research_loop(
                "매출액은?", Path("store"), model=model, max_rounds=1, target=target,
            )
        tool_cls.assert_called_once_with(Path("store"), target)
        self.assertEqual(result["collection"], target.collection_name)


if __name__ == "__main__":
    unittest.main()
