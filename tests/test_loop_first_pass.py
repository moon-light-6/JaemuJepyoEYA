"""모의 모델을 사용해 출처의 유효성과 1차 조사 흐름을 검사합니다."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fin_research.research.loop.findings import GroundedAnswer, generate_findings
from fin_research.research.loop.graph import run_once
from fin_research.tools.doc_search_tool import search_documents


EVIDENCE = {
    "evidence_id": "S1", "chunk_id": "chunk_1",
    "text": "2025년 연결 손익계산서, 단위 백만원. 매출액 예시.",
    "source_file": "example.pdf", "pdf_page": 85,
    "metadata": {"statement_scope": "consolidated", "fiscal_year": 2025},
}


def fake_model(answer):
    model = Mock()
    model.with_structured_output.return_value = RunnableLambda(lambda prompt: answer)
    return model


class FirstPassTests(unittest.TestCase):
    def test_valid_citations(self):
        answer = GroundedAnswer(
            status="draft", answer="표의 단위는 백만원입니다. [S1]",
            evidence_ids=["S1"], limitations=["수치 검증은 별도 수행"],
        )
        result = generate_findings("단위는?", [EVIDENCE], fake_model(answer))
        self.assertEqual(result.evidence_ids, ["S1"])

    def test_fabricated_citation_is_rejected(self):
        answer = GroundedAnswer(
            status="draft", answer="답변 [S99]", evidence_ids=["S99"], limitations=[],
        )
        with self.assertRaisesRegex(ValueError, "없는 출처"):
            generate_findings("질문", [EVIDENCE], fake_model(answer))

    def test_unlisted_inline_citation_is_rejected(self):
        answer = GroundedAnswer(
            status="draft", answer="답변 [S1]", evidence_ids=[], limitations=[],
        )
        with self.assertRaisesRegex(ValueError, "일치하지"):
            generate_findings("질문", [EVIDENCE], fake_model(answer))

    def test_uncited_draft_is_rejected(self):
        answer = GroundedAnswer(
            status="draft", answer="출처 없는 답변", evidence_ids=[], limitations=[],
        )
        with self.assertRaisesRegex(ValueError, "출처가 없습니다"):
            generate_findings("질문", [EVIDENCE], fake_model(answer))

    def test_empty_search_does_not_call_model(self):
        model = Mock()
        result = generate_findings("질문", [], model)
        self.assertEqual(result.status, "insufficient_evidence")
        model.with_structured_output.assert_not_called()

    @patch("fin_research.research.loop.graph.execute_search", return_value=[EVIDENCE])
    @patch("fin_research.llm.router.get_llm_for_role")
    def test_search_only_does_not_create_gpt_client(self, get_model, search):
        result = run_once("질문", Path("unused"), search_only=True)
        get_model.assert_not_called()
        search.assert_called_once()
        self.assertIsNone(result["findings"])
        self.assertEqual(result["numeric_verification"], "not_performed")

    @patch("fin_research.research.loop.graph.execute_search")
    @patch("fin_research.llm.router.get_llm_for_role", side_effect=RuntimeError("키 없음"))
    def test_missing_key_fails_before_embedding_load(self, get_model, search):
        with self.assertRaisesRegex(RuntimeError, "키 없음"):
            run_once("질문", Path("unused"))
        search.assert_not_called()

    @patch("fin_research.research.loop.graph.execute_search", return_value=[EVIDENCE])
    @patch("fin_research.llm.router.get_llm_for_role")
    def test_gpt_path_returns_draft_with_search_sources(self, get_model, search):
        answer = GroundedAnswer(
            status="draft", answer="단위는 백만원입니다. [S1]",
            evidence_ids=["S1"], limitations=[],
        )
        model = fake_model(answer)
        model.model_name = "test-model"
        get_model.return_value = model
        result = run_once("단위는?", Path("unused"))
        self.assertEqual(result["findings"]["status"], "draft")
        self.assertEqual(result["findings"]["evidence_ids"], ["S1"])
        self.assertEqual(result["evidence_count"], 1)
        self.assertEqual(result["model"], "test-model")

    def test_missing_index_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            search_documents("질문", Path("missing_index_for_test"))

    def test_empty_question_is_rejected(self):
        with self.assertRaises(ValueError):
            run_once("  ", Path("unused"), search_only=True)


if __name__ == "__main__":
    unittest.main()
