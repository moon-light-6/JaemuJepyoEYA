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

        self.collection_name = (
            "fin_00126380_2025_consolidated_20260310002820_kure_v1"
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
            collection_name=self.collection_name,
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
            collection_name=self.collection_name,
        )

        self.vectorstore.as_retriever.assert_called_once_with(
            search_type="mmr",
            search_kwargs={
                "k": 3,
                "fetch_k": 10,
                "lambda_mult": 0.7,
            },
        )

    # 3. 컬렉션 전달 여부 확인
    def test_mmr_collection_name(self):
        search_mmr(
            self.persist_directory,
            "매출액",
            collection_name=self.collection_name,
        )

        self.vectorstore.as_retriever.assert_called_once()

        # 실제 create_chroma() 대신 사용한 Mock 호출 검증
        with patch(
            "fin_research.retrieval.chroma_retriever.create_chroma"
        ) as mock_create:
            mock_create.return_value = self.vectorstore

            search_mmr(
                self.persist_directory,
                "매출액",
                collection_name=self.collection_name,
            )

            mock_create.assert_called_once_with(
                persist_directory=self.persist_directory,
                collection_name=self.collection_name,
            )

    # 4. 빈 질문 처리
    def test_empty_question(self):
        with self.assertRaises(ValueError):
            search_mmr(
                self.persist_directory,
                "   ",
                collection_name=self.collection_name,
            )

    # 5. 잘못된 k 처리
    def test_invalid_k(self):
        with self.assertRaises(ValueError):
            search_mmr(
                self.persist_directory,
                "매출액",
                k=0,
                collection_name=self.collection_name,
            )

    # 6. fetch_k가 k보다 작은 경우
    def test_invalid_fetch_k(self):
        with self.assertRaises(ValueError):
            search_mmr(
                self.persist_directory,
                "매출액",
                k=5,
                fetch_k=3,
                collection_name=self.collection_name,
            )

    # 7. lambda_mult 범위 검사
    def test_invalid_lambda_mult(self):
        for value in [-0.1, 1.1]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    search_mmr(
                        self.persist_directory,
                        "매출액",
                        lambda_mult=value,
                        collection_name=self.collection_name,
                    )

    # 8. 비어 있는 컬렉션 검색 방지
    def test_search_without_index(self):
        self.vectorstore._collection.count.return_value = 0

        with self.assertRaises(ValueError):
            search_mmr(
                self.persist_directory,
                "매출액",
                collection_name=self.collection_name,
            )


if __name__ == "__main__":
    unittest.main()
