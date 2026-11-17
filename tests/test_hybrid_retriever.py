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

from langchain_core.documents import Document
from fin_research.retrieval.search_target import SearchTarget


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

        self.target = SearchTarget(
            corp_code="00126380",
            fiscal_year=2025,
            statement_scope="consolidated",
            rcept_no="20260310002820",
        )

        self.project_root = Path("test_project")
        self.persist_directory = "test_chroma"

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
                target=self.target,
                project_root=self.project_root,
                persist_directory=self.persist_directory,
                question="   ",
            )

    # 8. candidate_k 검사
    def test_invalid_candidate_k(self):
        with self.assertRaises(ValueError):
            search_hybrid(
                target=self.target,
                project_root=self.project_root,
                persist_directory=self.persist_directory,
                question="매출액",
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
        # 실제 청크와 같은 메타데이터 구조
        test_document = Document(
            page_content="삼성전자 2025년 매출액",
            metadata={
                "chunk_id": "chunk_001",
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
                "source_file": "20260310002820.pdf",
                "pdf_page": 85,
                "section": "연결재무제표",
            },
        )

        mock_load.return_value = [test_document]
        mock_build.return_value = "test_retriever"

        mock_bm25.return_value = [
            self.doc_b,
            self.doc_a,
        ]

        # Chroma 검색 결과에 현재 검색 대상 메타데이터 추가
        chroma_a = dict(self.doc_a)
        chroma_a["metadata"] = dict(test_document.metadata)

        chroma_c = dict(self.doc_c)
        chroma_c["metadata"] = dict(test_document.metadata)

        mock_chroma.return_value = [
            chroma_a,
            chroma_c,
        ]

        results = search_hybrid(
            target=self.target,
            project_root=self.project_root,
            persist_directory=self.persist_directory,
            question="매출액",
            k=2,
            candidate_k=3,
        )

        # BM25와 Chroma에 모두 등장한 chunk_001이 1위
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["chunk_id"], "chunk_001")

        mock_load.assert_called_once_with(
            self.target.chunks_path(self.project_root)
        )

        mock_build.assert_called_once_with(
            [test_document],
            k=3,
        )

        mock_bm25.assert_called_once_with(
            "test_retriever",
            "매출액",
        )

        mock_chroma.assert_called_once_with(
            persist_directory=self.persist_directory,
            question="매출액",
            collection_name=self.target.collection_name,
            k=3,
        )

    @patch("fin_research.retrieval.hybrid_retriever.search_chroma")
    @patch("fin_research.retrieval.hybrid_retriever.search_bm25")
    @patch("fin_research.retrieval.hybrid_retriever.build_bm25_retriever")
    @patch("fin_research.retrieval.hybrid_retriever.load_chunk_documents")
    def test_reject_different_chroma_target(
        self,
        mock_load,
        mock_build,
        mock_bm25,
        mock_chroma,
    ):
        mock_load.return_value = [
            Document(
                page_content="2025년 매출액",
                metadata={
                    "chunk_id": "chunk_001",
                    "fiscal_year": 2025,
                    "statement_scope": "consolidated",
                    "source_file": "20260310002820.pdf",
                },
            )
        ]

        mock_bm25.return_value = [self.doc_a]

        # 연결재무제표를 요청했는데 별도재무제표가 검색된 상황
        invalid_result = dict(self.doc_b)
        invalid_result["metadata"] = {
            "fiscal_year": 2025,
            "statement_scope": "separate",
            "source_file": "20260310002820.pdf",
        }

        mock_chroma.return_value = [invalid_result]

        with self.assertRaisesRegex(
            ValueError,
            "Chroma 검색 결과",
        ):
            search_hybrid(
                target=self.target,
                project_root=self.project_root,
                persist_directory=self.persist_directory,
                question="매출액",
                k=1,
                candidate_k=2,
            )


if __name__ == "__main__":
    unittest.main()
