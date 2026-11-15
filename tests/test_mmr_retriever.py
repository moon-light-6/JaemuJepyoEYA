"""MMR 검색기의 기본 기능 및 예외 처리 단위 테스트."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.chroma_retriever import search_mmr


class MMRRetrieverTest(unittest.TestCase):

    def setUp(self):
        self.persist_directory = "test_chroma"

        self.document = Document(
            page_content="2025년 연결 매출액",
            metadata={
                "chunk_id": "chunk_001",
                "source_file": "test.pdf",
                "pdf_page": 85,
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
            },
        )

        # 실제 ChromaDB 대신 가짜 검색기 사용
        self.vectorstore = MagicMock()
        self.vectorstore._collection.count.return_value = 3

        self.retriever = MagicMock()
        self.retriever.invoke.return_value = [self.document]

        self.vectorstore.as_retriever.return_value = self.retriever

        self.chroma_patch = patch(
            "fin_research.retrieval.chroma_retriever.create_chroma",
            return_value=self.vectorstore,
        )
        self.chroma_patch.start()

    def tearDown(self):
        self.chroma_patch.stop()

    # 1. MMR 검색 결과와 출처 확인
    def test_mmr_search(self):
        results = search_mmr(
            self.persist_directory,
            "2025년 매출액",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["chunk_id"], "chunk_001")
        self.assertEqual(results[0]["pdf_page"], 85)
        self.assertEqual(results[0]["source_file"], "test.pdf")

    # 2. MMR 검색 옵션 확인
    def test_mmr_options(self):
        search_mmr(
            self.persist_directory,
            "매출액",
            k=3,
            fetch_k=10,
            lambda_mult=0.7,
        )

        self.vectorstore.as_retriever.assert_called_once_with(
            search_type="mmr",
            search_kwargs={
                "k": 3,
                "fetch_k": 10,
                "lambda_mult": 0.7,
            },
        )

    # 3. 빈 질문 처리
    def test_empty_question(self):
        with self.assertRaises(ValueError):
            search_mmr(self.persist_directory, "   ")

    # 4. 잘못된 k 처리
    def test_invalid_k(self):
        with self.assertRaises(ValueError):
            search_mmr(self.persist_directory, "매출액", k=0)

    # 5. fetch_k가 k보다 작은 경우
    def test_invalid_fetch_k(self):
        with self.assertRaises(ValueError):
            search_mmr(
                self.persist_directory,
                "매출액",
                k=5,
                fetch_k=3,
            )

    # 6. lambda_mult 범위 검사
    def test_invalid_lambda_mult(self):
        for value in [-0.1, 1.1]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    search_mmr(
                        self.persist_directory,
                        "매출액",
                        lambda_mult=value,
                    )

    # 7. 비어 있는 컬렉션 검색 방지
    def test_search_without_index(self):
        self.vectorstore._collection.count.return_value = 0

        with self.assertRaises(ValueError):
            search_mmr(self.persist_directory, "매출액")


if __name__ == "__main__":
    unittest.main()
