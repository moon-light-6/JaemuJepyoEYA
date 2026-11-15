"""검색 성능 평가 실행 및 결과 저장 단위 테스트."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCRIPT_PATH = PROJECT_ROOT / "scripts" / "evaluate_retrieval.py"

spec = importlib.util.spec_from_file_location(
    "evaluate_retrieval_script", SCRIPT_PATH
)
evaluate_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate_script)


def make_document(chunk_id, page):
    """평가용 가상 청크를 생성한다."""
    return Document(
        page_content=f"테스트 청크 {chunk_id}",
        metadata={
            "chunk_id": chunk_id,
            "source_file": "test.pdf",
            "pdf_page": page,
        },
    )


def make_result(document, rank=1):
    """BM25 검색 결과 형식으로 변환한다."""
    return {
        "rank": rank,
        "chunk_id": document.metadata["chunk_id"],
        "text": document.page_content,
        "source_file": document.metadata["source_file"],
        "pdf_page": document.metadata["pdf_page"],
        "metadata": document.metadata,
    }


class EvaluateRetrievalTest(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.doc_85 = make_document("chunk_001", 85)
        self.doc_83 = make_document("chunk_002", 83)
        self.doc_125 = make_document("chunk_003", 125)

        self.documents = [
            self.doc_85,
            self.doc_83,
            self.doc_125,
        ]

        self.questions = [
            {
                "id": "S01",
                "question": "매출액은?",
                "answer_type": "numeric",
                "category": "income_statement",
                "gold_sources": [
                    {"source_file": "test.pdf", "pdf_page": 85}
                ],
            },
            {
                "id": "S02",
                "question": "자산총계는?",
                "answer_type": "numeric",
                "category": "financial_position",
                "gold_sources": [
                    {"source_file": "test.pdf", "pdf_page": 83}
                ],
            },
        ]

        self.vectorstore = MagicMock()
        self.vectorstore._collection.count.return_value = 3
        self.vectorstore.get.return_value = {
            "ids": ["chunk_001", "chunk_002", "chunk_003"]
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_evaluation(self):
        """외부 모델 없이 평가 흐름을 실행한다."""

        with (
            patch.object(
                evaluate_script,
                "load_eval_dataset",
                return_value=self.questions,
            ),
            patch.object(
                evaluate_script,
                "load_chunk_documents",
                return_value=self.documents,
            ),
            patch.object(
                evaluate_script,
                "build_bm25_retriever",
                return_value=MagicMock(),
            ),
            patch.object(
                evaluate_script,
                "search_bm25",
                return_value=[make_result(self.doc_125)],
            ),
            patch.object(
                evaluate_script,
                "create_chroma",
                return_value=self.vectorstore,
            ),
        ):
            self.vectorstore.similarity_search_with_score.side_effect = [
                [(self.doc_85, 0.2)],
                [(self.doc_83, 0.3)],
            ]

            self.vectorstore.max_marginal_relevance_search.side_effect = [
                [self.doc_85],
                [self.doc_125],
            ]

            return evaluate_script.evaluate_retrievers(
                dataset_path=self.root / "eval.json",
                chunks_path=self.root / "chunks.json",
                chroma_path=self.root / "chroma",
            )

    # 1. 네 가지 검색 방식 평가
    def test_all_retrievers_evaluated(self):
        report = self.run_evaluation()

        self.assertEqual(
            set(report["summary"]),
            {"bm25", "chroma", "mmr", "hybrid"},
        )

        for method in report["summary"]:
            self.assertEqual(report["summary"][method]["total"], 2)
            self.assertEqual(len(report["details"][method]), 2)

    # 2. Hit@3·Hit@5 계산 확인
    def test_hit_rate_summary(self):
        report = self.run_evaluation()
        summary = report["summary"]

        self.assertEqual(summary["bm25"]["hit_at_5"], 0.0)
        self.assertEqual(summary["chroma"]["hit_at_3"], 1.0)
        self.assertEqual(summary["chroma"]["hit_at_5"], 1.0)
        self.assertEqual(summary["mmr"]["hit_at_5"], 0.5)
        self.assertEqual(summary["hybrid"]["hit_at_5"], 1.0)

        self.assertEqual(summary["chroma"]["hits_at_5"], 2)

    # 3. Chroma 청크 수 불일치 검사
    def test_chroma_count_mismatch(self):
        self.vectorstore._collection.count.return_value = 2

        with self.assertRaises(ValueError):
            self.run_evaluation()

    # 4. Chroma 청크 ID 불일치 검사
    def test_chroma_id_mismatch(self):
        self.vectorstore.get.return_value = {
            "ids": ["chunk_001", "chunk_002", "other_003"]
        }

        with self.assertRaises(ValueError):
            self.run_evaluation()

    # 5. 평가 결과 JSON 저장
    def test_json_output(self):
        output_path = self.root / "results" / "baseline.json"

        fake_report = {
            "config": {"questions": 2, "chunks": 3},
            "summary": {
                "chroma": {"hit_at_3": 1.0, "hit_at_5": 1.0}
            },
            "details": {"chroma": []},
        }

        argv = [
            "evaluate_retrieval.py",
            "--output-path",
            str(output_path),
        ]

        with (
            patch.object(sys, "argv", argv),
            patch.object(
                evaluate_script,
                "evaluate_retrievers",
                return_value=fake_report,
            ),
        ):
            evaluate_script.main()

        self.assertTrue(output_path.exists())

        saved = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(saved, fake_report)
        self.assertEqual(saved["summary"]["chroma"]["hit_at_5"], 1.0)


if __name__ == "__main__":
    unittest.main()
