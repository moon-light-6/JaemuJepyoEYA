"""재검색의 하이브리드 결합·공시 범위·v2 색인 선택을 확인한다."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from langchain_core.documents import Document
from fin_research.retrieval.search_target import SearchTarget
from fin_research.tools.doc_search_tool import XMLDocumentSearchTool
from fin_research.research.loop.graph import evidence_location

TARGET = SearchTarget("00000001", 2025, "consolidated", "20260301000001")


def document(index, text="매출 감소 원인", receipt=TARGET.rcept_no):
    """기업·공시·XPath를 포함한 검색 대역 문단을 만든다."""
    return Document(page_content=text, metadata={
        "chunk_id": f"00000001_2025_consolidated_xml_{index:04d}_01",
        "source_file": f"{receipt}_01.xml", "source_xpath": f"/DOCUMENT/P[{index}]",
        "source_element_index": index, "title": "주석", "source_format": "xml",
        "fiscal_year": 2025, "statement_scope": "consolidated", "section": "연결재무제표 주석",
    })


class XMLRetrySearchTests(unittest.TestCase):
    """임베딩 모델을 호출하지 않고 검색 경로와 출처 검사를 검증한다."""

    def setUp(self):
        """임시 색인 표시 파일과 벡터 저장소 대역을 준비한다."""
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "chroma.sqlite3").touch()
        self.tool = XMLDocumentSearchTool(self.root, TARGET)
        self.store = Mock()
        self.store._collection.count.return_value = 2
        self.tool._vectorstore = self.store

    def test_retry_combines_keyword_hits_and_prioritizes_unseen_evidence(self):
        """첫 검색의 동일 청크보다 재검색에서 확보한 새 키워드 근거를 먼저 반환한다."""
        old, new = document(1, "기업 개요"), document(2)
        self.store.similarity_search_with_score.return_value = [(old, 0.1)]
        self.assertEqual(self.tool.search("매출 감소", 1)[0]["chunk_id"], old.metadata["chunk_id"])
        with patch.object(self.tool, "_lexical_hits", return_value=[new]):
            hits = self.tool.search("매출 감소 원인", 1)
        self.assertEqual(hits[0]["chunk_id"], new.metadata["chunk_id"])
        self.assertEqual(hits[0]["source_xpath"], "/DOCUMENT/P[2]")
        self.assertEqual(self.tool.last_search_info["strategy"], "hybrid_notes")
        self.assertIn({"section": "연결재무제표 주석"}, self.store.similarity_search_with_score.call_args.kwargs["filter"]["$and"])

    def test_lexical_cache_filters_zero_overlap_and_reuses_collection(self):
        """필터별 BM25를 재사용하고 질문 토큰이 전혀 없는 후보는 추가하지 않는다."""
        candidate = document(1, "매출 감소")
        self.store._collection.get.return_value = {"documents": [candidate.page_content], "metadatas": [candidate.metadata]}
        where = self.tool._filter()
        self.assertEqual(len(self.tool._lexical_hits("매출", where, 3)), 1)
        self.assertEqual(self.tool._lexical_hits("관계없는질문", where, 3), [])
        self.store._collection.get.assert_called_once()

    def test_explicit_section_is_never_relaxed(self):
        """사용자가 지정한 섹션이 비어도 다른 섹션을 검색하지 않는다."""
        self.tool.section = "사용자 지정 주석"
        self.store.similarity_search_with_score.return_value = []
        self.tool.search("매출")
        with patch.object(self.tool, "_lexical_hits", return_value=[]):
            self.tool.search("수익")
        self.assertEqual(self.store.similarity_search_with_score.call_count, 2)
        for call in self.store.similarity_search_with_score.call_args_list:
            self.assertIn({"section": "사용자 지정 주석"}, call.kwargs["filter"]["$and"])

    def test_wrong_receipt_is_rejected(self):
        """비슷한 내용이어도 다른 공시의 검색 결과를 근거로 사용하지 않는다."""
        self.store.similarity_search_with_score.return_value = [(document(1, receipt="20260301000002"), 0.1)]
        with self.assertRaisesRegex(ValueError, "접수번호"):
            self.tool.search("매출")

    def test_v2_resolves_digest_and_requires_existing_index(self):
        """v2 파이프라인과 동일한 청크 해시 폴더를 선택하고 v1로 되돌아가지 않는다."""
        chunks = TARGET.xml_scope_dir(self.root) / f"{TARGET.rcept_no}_xml_chunks_v2.json"
        chunks.parent.mkdir(parents=True)
        chunks.write_bytes(b"[]")
        tool = XMLDocumentSearchTool(self.root / "chroma_v2", TARGET, chunk_version=2, project_root=self.root)
        self.assertEqual(tool.persist_directory, self.root / "chroma_v2" / hashlib.sha256(b"[]").hexdigest())
        with self.assertRaises(FileNotFoundError):
            tool.search("매출")
        chunks.unlink()
        with self.assertRaises(FileNotFoundError):
            XMLDocumentSearchTool(self.root, TARGET, chunk_version=2, project_root=self.root)

    def test_xml_progress_uses_xpath_instead_of_missing_page(self):
        """XML 검색 로그에 None쪽 대신 실제 XPath가 표시되는지 확인한다."""
        self.assertEqual(evidence_location({"source_xpath": "/P[1]", "pdf_page": None}), "XPath /P[1]")
        self.assertEqual(evidence_location({"pdf_page": 7}), "7쪽")
        self.assertEqual(evidence_location({"pdf_page": None}), "위치 미확인")
