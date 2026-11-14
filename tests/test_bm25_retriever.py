""" """
import json
import sys
import tempfile
import unittest
from pathlib import Path

from langchain_core.documents import Document


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.bm25_retriever import (
    simple_tokens,
    load_chunk_documents,
    build_bm25_retriever,
    search_bm25,
)


class TestBM25Retriever(unittest.TestCase):

    def setUp(self):
        # 테스트 전용 가상 데이터
        self.chunks = [
            {
                "chunk_id": "chunk_001",
                "text": "매출액 영업이익 수익 수익",
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
                "source_file": "test.pdf",
                "pdf_page": 85,
                "section": "연결재무제표",
            },
            {
                "chunk_id": "chunk_002",
                "text": "자산총계 부채총계 자본총계",
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
                "source_file": "test.pdf",
                "pdf_page": 83,
                "section": "연결재무제표",
            },
            {
                "chunk_id": "chunk_003",
                "text": "재고자산 평가 회계정책",
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
                "source_file": "test.pdf",
                "pdf_page": 110,
                "section": "연결재무제표 주석",
            },
        ]

        self.temp_dir = tempfile.TemporaryDirectory()
        self.chunks_path = Path(self.temp_dir.name) / "test_chunks.json"

        self.chunks_path.write_text(
            json.dumps(self.chunks, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    # 1. 토큰화 테스트
    def test_simple_tokens(self):
        tokens = simple_tokens("삼성전자 2025년 매출액은 100억원!")

        self.assertEqual(
            tokens,
            ["삼성전자", "2025년", "매출액은", "100억원"],
        )

    # 2. JSON → Document 변환 테스트
    def test_load_chunk_documents(self):
        documents = load_chunk_documents(self.chunks_path)

        self.assertEqual(len(documents), 3)
        self.assertIsInstance(documents[0], Document)
        self.assertEqual(
            documents[0].page_content,
            self.chunks[0]["text"],
        )

    # 3. 메타데이터 유지 테스트
    def test_metadata_preserved(self):
        documents = load_chunk_documents(self.chunks_path)

        self.assertEqual(documents[0].metadata["chunk_id"], "chunk_001")
        self.assertEqual(documents[0].metadata["pdf_page"], 85)
        self.assertEqual(
            documents[0].metadata["source_file"],
            "test.pdf",
        )
        self.assertEqual(
            documents[0].metadata["statement_scope"],
            "consolidated",
        )

    # 4. BM25 검색 테스트
    def test_bm25_search(self):
        documents = load_chunk_documents(self.chunks_path)
        retriever = build_bm25_retriever(documents, k=2)

        results = search_bm25(retriever, "매출액 수익")

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["chunk_id"], "chunk_001")
        self.assertEqual(results[0]["pdf_page"], 85)

    # 5. Top-K 테스트
    def test_top_k(self):
        documents = load_chunk_documents(self.chunks_path)
        retriever = build_bm25_retriever(documents, k=2)

        results = search_bm25(retriever, "자산총계")

        self.assertEqual(len(results), 2)

    # 6. 빈 질문 처리
    def test_empty_question(self):
        documents = load_chunk_documents(self.chunks_path)
        retriever = build_bm25_retriever(documents)

        with self.assertRaises(ValueError):
            search_bm25(retriever, "   ")

    # 7. 잘못된 k 처리
    def test_invalid_k(self):
        documents = load_chunk_documents(self.chunks_path)

        with self.assertRaises(ValueError):
            build_bm25_retriever(documents, k=0)

    # 8. 필수 필드 누락
    def test_missing_field(self):
        invalid_chunks = [dict(self.chunks[0])]
        del invalid_chunks[0]["pdf_page"]

        self.chunks_path.write_text(
            json.dumps(invalid_chunks, ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            load_chunk_documents(self.chunks_path)

    # 9. 빈 청크 JSON
    def test_empty_chunks(self):
        self.chunks_path.write_text("[]", encoding="utf-8")

        with self.assertRaises(ValueError):
            load_chunk_documents(self.chunks_path)


if __name__ == "__main__":
    unittest.main()
