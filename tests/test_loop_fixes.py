"""루프 보정: 평가 모순을 예외 대신 보정(부분 답변)하고, pdf_page가 없는 XML 근거로도 루프가 도는지 검증합니다."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

from langchain_core.documents import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_loop_iterative import answer, evaluation, model_responses

from fin_research.research.loop.evaluator import EvidenceEvaluation, normalize_evaluation
from fin_research.research.loop.graph import evidence_location, run_research_loop
from fin_research.retrieval.search_target import SearchTarget
from fin_research.tools.doc_search_tool import DocumentSearchTool

TARGET = SearchTarget("00266961", 2025, "consolidated", "20260313001021")


def xml_hit(number, title="연결 현금흐름표"):
    """XML 색인 검색 결과: pdf_page가 None이고 source_xpath·title이 있다."""
    return {"evidence_id": "S1", "chunk_id": f"xml_{number}", "text": f"영업활동현금흐름 | {number}",
            "source_file": "20260313001021_00761.xml", "pdf_page": None,
            "source_xpath": f"/DOCUMENT[1]/TABLE[{number}]", "title": title, "metadata": {}}


def ev(sufficient, ids, missing, next_query=None):
    return EvidenceEvaluation(sufficient=sufficient, reason="평가", evidence_ids=ids,
                              missing_items=missing, next_query=next_query)


class TestNormalizeEvaluation(unittest.TestCase):
    def test_consistent_result_unchanged(self):
        result = ev(True, ["S1"], [])
        self.assertIs(normalize_evaluation(result, {"S1"}), result)

    def test_sufficient_with_missing_items_remains_unfinished(self):
        # 어제 현금흐름 사례: 현금흐름표는 찾았지만 '재무활동 현금흐름'을 못 찾았다고 함께 답함
        result = normalize_evaluation(ev(True, ["S1"], ["재무활동 현금흐름"]), {"S1"})
        self.assertFalse(result.sufficient)
        self.assertEqual(result.missing_items, ["재무활동 현금흐름"])

    def test_unknown_ids_dropped_but_known_kept(self):
        result = normalize_evaluation(ev(True, ["S1", "S9"], []), {"S1"})
        self.assertTrue(result.sufficient)
        self.assertEqual(result.evidence_ids, ["S1"])
        self.assertIn("S9 제외", result.reason)

    def test_sufficient_without_usable_source_becomes_insufficient(self):
        result = normalize_evaluation(ev(True, ["S9"], [], next_query=None), {"S1"})
        self.assertFalse(result.sufficient)
        self.assertEqual(result.evidence_ids, [])
        self.assertTrue(result.missing_items)


class TestPartialAnswerInLoop(unittest.TestCase):
    def test_partial_answer_keeps_found_evidence_and_lists_missing(self):
        partial = {**evaluation(True, ids=["S1"]), "missing_items": ["재무활동 현금흐름"]}
        result = run_research_loop("현금흐름 각각 얼마인가?", Path("unused"), max_rounds=2,
                                   model=model_responses(partial, answer("S1")),
                                   search_function=Mock(return_value=[xml_hit(1)]))
        self.assertEqual(result["findings"]["status"], "insufficient_evidence")
        self.assertEqual(len(result["evidences"]), 1)
        self.assertIn("재무활동 현금흐름", result["findings"]["limitations"])


class TestXmlEvidenceInLoop(unittest.TestCase):
    def test_loop_runs_with_xml_hits_and_reports_title(self):
        messages = []
        result = run_research_loop("영업활동현금흐름은?", Path("unused"), max_rounds=2,
                                   model=model_responses(evaluation(), evaluation(True, ids=["S2"]), answer("S2")),
                                   search_function=Mock(side_effect=[[xml_hit(1)], [xml_hit(2)]]),
                                   progress=messages.append)
        self.assertEqual(result["findings"]["status"], "draft")
        # loop/gunhee 병합: 진행 메시지는 표 제목 대신 XPath로 위치를 보여 준다
        self.assertTrue(any("XPath" in m and "· 청크 xml_2" in m for m in messages))

    def test_location(self):
        # loop/gunhee 병합: 진행 메시지 위치는 건희의 evidence_location (XPath 우선)
        self.assertEqual(evidence_location({"pdf_page": 84}), "84쪽")
        self.assertEqual(evidence_location({"source_xpath": "/D[1]"}), "XPath /D[1]")
        self.assertEqual(evidence_location({}), "위치 미확인")


class TestDocumentSearchToolSource(unittest.TestCase):
    def test_collection_by_source(self):
        self.assertEqual(DocumentSearchTool(Path("."), TARGET).collection_name, TARGET.collection_name)
        self.assertEqual(DocumentSearchTool(Path("."), TARGET, "xml").collection_name, TARGET.xml_collection_name)
        self.assertTrue(TARGET.xml_collection_name.startswith("fin_xml_"))

    def test_invalid_source(self):
        with self.assertRaises(ValueError):
            DocumentSearchTool(Path("."), TARGET, "html")
        with self.assertRaises(ValueError):
            DocumentSearchTool(Path("."), None, "xml")


class TestChromaResultWithoutPdfPage(unittest.TestCase):
    def test_xml_metadata_without_pdf_page(self):
        from fin_research.retrieval import chroma_retriever

        doc = Document(page_content="영업활동현금흐름 | 3,100,000",
                       metadata={"chunk_id": "x1", "source_file": "a.xml", "source_xpath": "/D[1]",
                                 "title": "연결 현금흐름표"})
        store = Mock()
        store.similarity_search_with_score.return_value = [(doc, 0.2)]
        results = chroma_retriever.search_chroma(Path("."), "현금흐름", k=1, vectorstore=store)
        self.assertEqual((results[0]["pdf_page"], results[0]["source_xpath"], results[0]["title"]),
                         (None, "/D[1]", "연결 현금흐름표"))


if __name__ == "__main__":
    unittest.main()


class TestNormalizeCitations(unittest.TestCase):
    """findings 출처 검사: 지어낸 인용은 오류, 형식만 어긋난 맞는 답은 바로잡는다."""

    def setUp(self):
        from fin_research.research.loop.findings import GroundedAnswer, normalize_citations
        self.make = lambda answer_, ids, status="draft": GroundedAnswer(
            status=status, answer=answer_, evidence_ids=ids, limitations=[])
        self.normalize = normalize_citations

    def test_missing_inline_citation_is_appended(self):
        # 실제 NAVER 사례: 숫자는 맞는데 본문에 [S1]을 빠뜨림
        result = self.normalize(self.make("영업활동 현금흐름은 3,095,806,604,677원입니다.", ["S1"]), {"S1", "S2"})
        self.assertEqual(result.answer, "영업활동 현금흐름은 3,095,806,604,677원입니다. [S1]")
        self.assertEqual(result.evidence_ids, ["S1"])

    def test_grouped_citation_is_read(self):
        result = self.normalize(self.make("매출 12조, 이익 2.2조 [S1, S3].", ["S1", "S3"]), {"S1", "S3"})
        self.assertEqual(result.evidence_ids, ["S1", "S3"])

    def test_evidence_ids_follow_inline(self):
        result = self.normalize(self.make("이익 2.2조 [S3].", ["S1", "S3"]), {"S1", "S3"})
        self.assertEqual(result.evidence_ids, ["S3"])
        inline_only = self.normalize(self.make("이익 2.2조 [S2].", []), {"S2"})
        self.assertEqual(inline_only.evidence_ids, ["S2"])

    def test_product_name_is_not_citation(self):
        result = self.normalize(self.make("갤럭시 S25 판매 호조 [S1].", ["S1"]), {"S1"})
        self.assertEqual(result.evidence_ids, ["S1"])

    def test_fabricated_and_sourceless_still_fail(self):
        with self.assertRaisesRegex(ValueError, "S99"):
            self.normalize(self.make("자본총계 1억 [S99].", ["S99"]), {"S1"})
        with self.assertRaisesRegex(ValueError, "S9"):
            self.normalize(self.make("이익 [S1].", ["S1", "S9"]), {"S1"})
        with self.assertRaisesRegex(ValueError, "출처가 없습니다"):
            self.normalize(self.make("출처 없는 답변", []), {"S1"})

    def test_insufficient_without_source_is_fine(self):
        result = self.normalize(self.make("근거 부족", [], status="insufficient_evidence"), {"S1"})
        self.assertEqual(result.evidence_ids, [])

    def test_loop_answer_without_inline_citation_now_succeeds(self):
        no_inline = {"status": "draft", "answer": "영업활동 현금흐름은 3.1조원입니다.",
                     "evidence_ids": ["S1"], "limitations": []}
        result = run_research_loop("현금흐름은?", Path("unused"), max_rounds=1,
                                   model=model_responses(evaluation(True, ids=["S1"]), no_inline),
                                   search_function=Mock(return_value=[xml_hit(1)]))
        self.assertEqual(result["findings"]["status"], "draft")
        self.assertTrue(result["findings"]["answer"].endswith("[S1]"))
        self.assertEqual(result["llm_calls"], 2)  # 다시 요청하지 않고 바로잡음
