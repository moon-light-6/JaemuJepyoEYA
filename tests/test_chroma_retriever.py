"""Chroma 벡터 검색기의 기본 기능 및 예외 처리 단위 테스트."""

import json
import sys
import tempfile
import unittest
import uuid
import chromadb
from pathlib import Path
from unittest.mock import patch

from langchain_core.embeddings import Embeddings
from langchain_chroma import Chroma


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.chroma_retriever import (
    build_chroma,
    search_chroma,
)


class TestEmbeddings(Embeddings):
    """테스트용 임베딩 모델: 단어별로 고정된 벡터 생성."""

    # 실제 모델 대신 사용
    def _embed(self, text: str) -> list[float]:
        if "매출액" in text:
            return [1.0, 0.0, 0.0]
        if "자산총계" in text:
            return [0.0, 1.0, 0.0]
        if "재고자산" in text:
            return [0.0, 0.0, 1.0]
        return [0.1, 0.1, 0.1]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class TestChromaRetriever(unittest.TestCase):

    def setUp(self):
        # 테스트마다 독립된 임시 저장소 사용
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.chunks_path = self.root / "test_chunks.json"
        self.persist_directory = self.root / "chroma_db"

        self.chunks = [
            {
                "chunk_id": "chunk_001",
                "text": "매출액 영업수익",
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
                "source_file": "test.pdf",
                "pdf_page": 85,
                "section": "연결재무제표",
            },
            {
                "chunk_id": "chunk_002",
                "text": "자산총계 부채총계",
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
                "source_file": "test.pdf",
                "pdf_page": 83,
                "section": "연결재무제표",
            },
            {
                "chunk_id": "chunk_003",
                "text": "재고자산 회계정책",
                "company": "삼성전자",
                "fiscal_year": 2025,
                "statement_scope": "consolidated",
                "source_file": "test.pdf",
                "pdf_page": 110,
                "section": "연결재무제표 주석",
            },
        ]

        self._save_chunks()

        # 수정 전
        # # 실제 KURE-v1 로딩을 테스트용 임베딩으로 대체
        # self.embedding_patch = patch(
        #     "fin_research.retrieval.chroma_retriever.create_embedding_model",
        #     return_value=TestEmbeddings(),
        # )
        # self.embedding_patch.start()

        # 수정 후 1
        # 디스크 파일을 만들지 않는 테스트용 Chroma
        self.vectorstore = Chroma(
            collection_name=f"test_{uuid.uuid4().hex}",
            embedding_function=TestEmbeddings(),
            client=chromadb.EphemeralClient(),
        )

        # 수정 후 2
        # 실제 create_chroma() 대신 테스트용 Chroma 반환
        self.chroma_patch = patch(
            "fin_research.retrieval.chroma_retriever.create_chroma",
            return_value=self.vectorstore,
        )
        self.chroma_patch.start()

    # 수정 전
    # def tearDown(self):
    #     self.embedding_patch.stop()
    #     self.temp_dir.cleanup()

    # 수정 후
    def tearDown(self):
        self.chroma_patch.stop()
        self.vectorstore.delete_collection()
        self.temp_dir.cleanup()

    def _save_chunks(self):
        self.chunks_path.write_text(
            json.dumps(self.chunks, ensure_ascii=False),
            encoding="utf-8",
        )

    # 1. 최초 색인
    def test_initial_indexing(self):
        count = build_chroma(
            self.chunks_path,
            self.persist_directory,
        )

        self.assertEqual(count, 3)

        # 수정 전
        # self.assertTrue(self.persist_directory.exists())

        # 수정 후
        self.assertEqual(self.vectorstore._collection.count(), 3)

    # 2. 재실행 시 중복 적재 방지
    def test_reuse_existing_index(self):
        build_chroma(self.chunks_path, self.persist_directory)

        count = build_chroma(
            self.chunks_path,
            self.persist_directory,
        )

        self.assertEqual(count, 3)

    # 3. 관련 청크 검색 및 출처 확인
    def test_similarity_search(self):
        build_chroma(self.chunks_path, self.persist_directory)

        results = search_chroma(
            self.persist_directory,
            "매출액",
            k=2,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["chunk_id"], "chunk_001")
        self.assertEqual(results[0]["pdf_page"], 85)
        self.assertEqual(results[0]["source_file"], "test.pdf")
        self.assertIsInstance(results[0]["distance"], float)

    # 4. Top-K 결과 개수
    def test_top_k(self):
        build_chroma(self.chunks_path, self.persist_directory)

        for k in [1, 2, 3]:
            with self.subTest(k=k):
                results = search_chroma(
                    self.persist_directory,
                    "자산총계",
                    k=k,
                )

                self.assertEqual(len(results), k)

    # 5. 빈 질문 처리
    def test_empty_question(self):
        with self.assertRaises(ValueError):
            search_chroma(
                self.persist_directory,
                "   ",
            )

    # 6. 잘못된 k 처리
    def test_invalid_k(self):
        with self.assertRaises(ValueError):
            search_chroma(
                self.persist_directory,
                "매출액",
                k=0,
            )

    # 7. 미색인 저장소 검색 방지
    def test_search_without_index(self):
        with self.assertRaises(ValueError):
            search_chroma(
                self.persist_directory,
                "매출액",
            )

    # 8. 중복 chunk_id 검사
    def test_duplicate_chunk_id(self):
        duplicate = dict(self.chunks[0])
        self.chunks.append(duplicate)
        self._save_chunks()

        with self.assertRaises(ValueError):
            build_chroma(
                self.chunks_path,
                self.persist_directory,
            )

    # 9. 다른 청크 ID를 가진 기존 DB 재사용 방지
    def test_changed_chunk_ids(self):
        build_chroma(self.chunks_path, self.persist_directory)

        self.chunks[0]["chunk_id"] = "changed_001"
        self._save_chunks()

        with self.assertRaises(ValueError):
            build_chroma(
                self.chunks_path,
                self.persist_directory,
            )


if __name__ == "__main__":
    unittest.main()
