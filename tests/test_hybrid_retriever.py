"""Hybrid-RRF 검색기의 점수 계산 및 예외 처리 단위 테스트."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.hybrid_retriever import (
    rrf_merge,
    search_hybrid,
)


def make_result(chunk_id, pdf_page):
    """테스트용 검색 결과를 생성한다."""
    return {
        "rank": 1,
        "chunk_id": chunk_id,
        "text": f"{chunk_id} 본문",
        "source_file": "test.pdf",
        "pdf_page": pdf_page,
        "metadata": {
            "chunk_id": chunk_id,
            "source_file": "test.pdf",
            "pdf_page": pdf_page,
        },
    }


class HybridRetrieverTest(unittest.TestCase):

    def setUp(self):
        self.doc_a = make_result("chunk_001", 85)
        self.doc_b = make_result("chunk_002", 125)
        self.doc_c = make_result("chunk_003", 86)

    # 1. 동일 청크의 RRF 점수 합산
    def test_rrf_score(self):
        results = rrf_merge(
            [
                [self.doc_a, self.doc_b],
                [self.doc_b, self.doc_a],
            ]
        )

        self.assertEqual(len(results), 2)

        expected = 1 / 61 + 1 / 62

        self.assertAlmostEqual(
            results[0]["rrf_score"],
            expected,
        )
        self.assertAlmostEqual(
            results[1]["rrf_score"],
            expected,
        )

    # 2. 동일 chunk_id 중복 제거
    def test_duplicate_chunk_id(self):
        results = rrf_merge(
            [
                [self.doc_a, self.doc_a],
                [self.doc_a],
            ]
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["chunk_id"], "chunk_001")

        # 첫 번째 검색기 내부 중복은 점수에 한 번만 반영
        self.assertAlmostEqual(
            results[0]["rrf_score"],
            2 / 61,
        )

    # 3. RRF 점수에 따른 정렬
    def test_rrf_ranking(self):
        results = rrf_merge(
            [
                [self.doc_a, self.doc_b],
                [self.doc_b, self.doc_c],
            ]
        )

        self.assertEqual(results[0]["chunk_id"], "chunk_002")

    # 4. 동점인 경우 chunk_id 순으로 정렬
    def test_rrf_tie_break(self):
        results = rrf_merge(
            [
                [self.doc_b],
                [self.doc_a],
            ]
        )

        self.assertEqual(
            [result["chunk_id"] for result in results],
            ["chunk_001", "chunk_002"],
        )

    # 5. Top-K 적용
    def test_top_k(self):
        results = rrf_merge(
            [[self.doc_a, self.doc_b, self.doc_c]],
            top_k=2,
        )

        self.assertEqual(len(results), 2)

    # 6. 잘못된 RRF 설정 검사
    def test_invalid_rrf_parameters(self):
        with self.assertRaises(ValueError):
            rrf_merge([[self.doc_a]], top_k=0)

        with self.assertRaises(ValueError):
            rrf_merge([[self.doc_a]], rank_constant=-1)

    # 7. 빈 질문 검사
    def test_empty_question(self):
        with self.assertRaises(ValueError):
            search_hybrid(
                "test_chunks.json",
                "test_chroma",
                "   ",
            )

    # 8. candidate_k 검사
    def test_invalid_candidate_k(self):
        with self.assertRaises(ValueError):
            search_hybrid(
                "test_chunks.json",
                "test_chroma",
                "매출액",
                k=5,
                candidate_k=3,
            )

    # 9. BM25와 Chroma 호출 및 결합 확인
    @patch("fin_research.retrieval.hybrid_retriever.search_chroma")
    @patch("fin_research.retrieval.hybrid_retriever.search_bm25")
    @patch("fin_research.retrieval.hybrid_retriever.build_bm25_retriever")
    @patch("fin_research.retrieval.hybrid_retriever.load_chunk_documents")
    def test_hybrid_search(
        self,
        mock_load,
        mock_build,
        mock_bm25,
        mock_chroma,
    ):
        mock_load.return_value = ["test_document"]
        mock_build.return_value = "test_retriever"

        mock_bm25.return_value = [
            self.doc_b,
            self.doc_a,
        ]
        mock_chroma.return_value = [
            self.doc_a,
            self.doc_c,
        ]

        results = search_hybrid(
            chunks_path="test_chunks.json",
            persist_directory="test_chroma",
            question="매출액",
            k=2,
            candidate_k=3,
        )

        # 두 검색기에서 모두 등장한 chunk_001이 1위
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["chunk_id"], "chunk_001")

        mock_load.assert_called_once_with("test_chunks.json")
        mock_build.assert_called_once_with(
            ["test_document"],
            k=3,
        )
        mock_bm25.assert_called_once_with(
            "test_retriever",
            "매출액",
        )
        mock_chroma.assert_called_once_with(
            persist_directory="test_chroma",
            question="매출액",
            k=3,
        )


if __name__ == "__main__":
    unittest.main()
