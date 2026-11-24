
"""기업별 Chroma 컬렉션 분리 테스트."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chromadb
from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.chroma_retriever import (
    build_chroma,
    create_chroma,
    search_chroma,
)


class FakeEmbeddings(Embeddings):
    """실제 KURE-v1 대신 사용하는 테스트 임베딩."""

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        if "매출액" in text:
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]


class TestChromaCollections(unittest.TestCase):

    def setUp(self):
        # 청크 JSON 파일을 위한 임시 폴더
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # Chroma는 디스크에 저장하지 않고 메모리에서만 실행
        self.client = chromadb.EphemeralClient()
        self.vectorstores = {}
        self.embeddings = FakeEmbeddings()

        self.samsung = SearchTarget(
            "00126380",
            2025,
            "consolidated",
            "20260310002820",
        )

        self.ecopro = SearchTarget(
            "01160363",
            2025,
            "consolidated",
            "20260318001622",
        )

        # 실제 create_chroma() 대신 동일한 테스트 클라이언트 사용
        def create_test_chroma(
            persist_directory,
            collection_name,
        ):
            # 컬렉션 이름별 Chroma 객체를 한 번만 생성
            if collection_name not in self.vectorstores:
                self.vectorstores[collection_name] = Chroma(
                    collection_name=collection_name,
                    embedding_function=self.embeddings,
                    client=self.client,
                )

            return self.vectorstores[collection_name]

        # 수정: 누락된 패치 설정 추가
        self.chroma_patch = patch(
            "fin_research.retrieval.chroma_retriever.create_chroma",
            side_effect=create_test_chroma,
        )
        self.chroma_patch.start()

    def tearDown(self):
        self.chroma_patch.stop()

        for collection_name in self.vectorstores:
            self.client.delete_collection(collection_name)

        self.vectorstores.clear()
        self.temp_dir.cleanup()

    def make_chunks(self, target, company, text):
        path = target.chunks_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)

        chunks = [
            {
                "chunk_id": "chunk_001",
                "text": text,
                "company": company,
                "fiscal_year": target.fiscal_year,
                "statement_scope": target.statement_scope,
                "source_file": f"{target.rcept_no}.pdf",
                "pdf_page": 1,
                "section": "연결재무제표",
            }
        ]

        path.write_text(
            json.dumps(chunks, ensure_ascii=False),
            encoding="utf-8",
        )

        return path

    def test_company_collections_are_isolated(self):
        # 1. 기업별 테스트 청크 생성
        samsung_path = self.make_chunks(
            self.samsung,
            "삼성전자",
            "삼성전자 매출액",
        )

        ecopro_path = self.make_chunks(
            self.ecopro,
            "에코프로비엠",
            "에코프로비엠 매출액",
        )

        # 2. 동일한 클라이언트에 각 기업 컬렉션 색인
        build_chroma(
            chunks_path=samsung_path,
            persist_directory=self.root / "chroma",
            collection_name=self.samsung.collection_name,
        )

        build_chroma(
            chunks_path=ecopro_path,
            persist_directory=self.root / "chroma",
            collection_name=self.ecopro.collection_name,
        )

        # 색인 직후 실제 저장 상태 확인
        samsung_count = self.client.get_collection(
            self.samsung.collection_name
        ).count()

        ecopro_count = self.client.get_collection(
            self.ecopro.collection_name
        ).count()

        print(f"삼성전자 실제 저장 수: {samsung_count}")
        print(f"에코프로비엠 실제 저장 수: {ecopro_count}")

        self.assertEqual(samsung_count, 1)
        self.assertEqual(ecopro_count, 1)

        # 3. 각 기업 컬렉션 검색
        samsung_results = search_chroma(
            persist_directory=self.root / "chroma",
            question="매출액",
            collection_name=self.samsung.collection_name,
            k=1,
        )

        ecopro_results = search_chroma(
            persist_directory=self.root / "chroma",
            question="매출액",
            collection_name=self.ecopro.collection_name,
            k=1,
        )

        # 4. 기업별 검색 결과 확인
        self.assertEqual(len(samsung_results), 1)
        self.assertEqual(len(ecopro_results), 1)

        self.assertEqual(
            samsung_results[0]["metadata"]["company"],
            "삼성전자",
        )

        self.assertEqual(
            ecopro_results[0]["metadata"]["company"],
            "에코프로비엠",
        )

        # 서로 같은 chunk_id를 사용해도 데이터가 섞이지 않아야 함
        self.assertEqual(
            samsung_results[0]["chunk_id"],
            ecopro_results[0]["chunk_id"],
        )

        self.assertNotEqual(
            samsung_results[0]["text"],
            ecopro_results[0]["text"],
        )

        # 5. 실제 컬렉션의 저장 청크 수 확인
        samsung_collection = self.client.get_collection(
            self.samsung.collection_name
        )
        ecopro_collection = self.client.get_collection(
            self.ecopro.collection_name
        )

        self.assertEqual(samsung_collection.count(), 1)
        self.assertEqual(ecopro_collection.count(), 1)

        # 6. 두 기업은 서로 다른 컬렉션이어야 함
        self.assertNotEqual(
            self.samsung.collection_name,
            self.ecopro.collection_name,
        )


if __name__ == "__main__":
    unittest.main()
