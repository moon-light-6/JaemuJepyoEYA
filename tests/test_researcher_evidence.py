"""researcher가 루프 근거를 장부(EvidenceRegistry)에 등록해 전역 S#로 바꾸는지 검증합니다 (LLM·검색 없음)."""

import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fin_research.evidence.registry import EvidenceRegistry
from fin_research.research.nodes.researcher import (
    make_loop_researcher,
    merge_loop_results,
    rewrite_citations,
    to_candidate,
)
from fin_research.research.state import Brief, SubTopic

XML = "20260313001021_00761.xml"


def hit(local_id, chunk_id, text="영업이익 | 2,208,138", **kw):
    """루프 결과의 evidences 한 건 (검색 결과 + 루프가 붙인 S#)."""
    return {"evidence_id": local_id, "source_file": XML, "chunk_id": chunk_id, "text": text,
            "pdf_page": None, "metadata": {"source_xpath": f"/DOCUMENT[1]/{chunk_id}", "title": "연결 손익계산서"},
            **kw}


def loop_result(answer, evidence_ids, evidences, limitations=()):
    return {"findings": {"status": "draft", "answer": answer, "evidence_ids": list(evidence_ids),
                         "limitations": list(limitations)}, "evidences": evidences}


def subtopic(id_="profitability", title="수익성"):
    return SubTopic(id=id_, title=title, questions=["질문1", "질문2"])


class TestRewriteCitations(unittest.TestCase):
    def test_single_and_grouped(self):
        mapping = {"S1": "S12", "S3": "S4"}
        self.assertEqual(rewrite_citations("영업이익 2.2조 [S1]. 매출 12조 [S1, S3].", mapping),
                         "영업이익 2.2조 [S12]. 매출 12조 [S12, S4].")

    def test_no_chained_replacement(self):
        # S1→S12, S12→S3을 순서대로 바꾸면 S1이 S3이 되어 버린다
        self.assertEqual(rewrite_citations("[S1] [S12]", {"S1": "S12", "S12": "S3"}), "[S12] [S3]")

    def test_unknown_ids_removed(self):
        self.assertEqual(rewrite_citations("자본총계 1억 [S9].", {}), "자본총계 1억.")
        self.assertEqual(rewrite_citations("값 [S1, S9]", {"S1": "S2"}), "값 [S2]")

    def test_to_candidate_reads_metadata(self):
        candidate = to_candidate(hit("S1", "c1"))
        self.assertEqual((candidate["source_xpath"], candidate["title"]), ("/DOCUMENT[1]/c1", "연결 손익계산서"))


class TestMergeWithRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = EvidenceRegistry()

    def test_local_ids_become_global(self):
        result = loop_result("영업이익은 2.2조원이다 [S1]. 순이익은 1.8조원 [S2].", ["S1", "S2"],
                             [hit("S1", "is-1"), hit("S2", "is-2", text="당기순이익 | 1,818,746")])
        finding = merge_loop_results(subtopic(), 1, [("질문1", result, None)], self.registry)
        self.assertEqual(finding.summary, "영업이익은 2.2조원이다 [S1]. 순이익은 1.8조원 [S2].")
        self.assertEqual(finding.evidence_ids, ["S1", "S2"])
        self.assertEqual([s.id for s in finding.sources], ["S1", "S2"])
        self.assertEqual(finding.sources[0].source_xpath, "/DOCUMENT[1]/is-1")
        self.assertEqual(finding.sources[0].title, "연결 손익계산서")

    def test_same_chunk_across_subtopics_gets_same_id(self):
        # 수익성 루프는 S1로, 안정성 루프는 S2로 같은 청크를 찾았다 → 장부에서는 둘 다 같은 번호
        first = merge_loop_results(subtopic(), 1, [("q", loop_result("A [S1]", ["S1"], [hit("S1", "is-1")]), None)],
                                   self.registry)
        other = loop_result("B [S1] C [S2]", ["S1", "S2"], [hit("S1", "bs-1"), hit("S2", "is-1")])
        second = merge_loop_results(subtopic("stability", "안정성"), 1, [("q", other, None)], self.registry)
        self.assertEqual(first.evidence_ids, ["S1"])
        self.assertEqual(second.summary, "B [S2] C [S1]")
        self.assertEqual(len(self.registry), 2)

    def test_same_evidence_from_two_questions_listed_once(self):
        r1 = loop_result("A [S1]", ["S1"], [hit("S1", "is-1")])
        r2 = loop_result("B [S1]", ["S1"], [hit("S1", "is-1")])
        finding = merge_loop_results(subtopic(), 1, [("q1", r1, None), ("q2", r2, None)], self.registry)
        self.assertEqual(finding.evidence_ids, ["S1"])
        self.assertEqual(len(finding.sources), 1)

    def test_fabricated_citation_is_dropped(self):
        result = loop_result("자본총계는 100,000,000원이다 [S9].", ["S9"], [hit("S1", "is-1")])
        finding = merge_loop_results(subtopic(), 1, [("q", result, None)], self.registry)
        self.assertEqual(finding.summary, "자본총계는 100,000,000원이다.")
        self.assertEqual(finding.evidence_ids, [])
        self.assertIn("검색 결과에 없는 근거 S9", finding.limitations[0])
        self.assertEqual(len(self.registry), 0)

    def test_cited_in_text_but_missing_from_evidence_ids(self):
        result = loop_result("A [S1] B [S2]", ["S1"], [hit("S1", "is-1"), hit("S2", "is-2")])
        finding = merge_loop_results(subtopic(), 1, [("q", result, None)], self.registry)
        self.assertEqual(finding.evidence_ids, ["S1", "S2"])

    def test_product_names_are_not_citations(self):
        # 루프가 근거를 25개 모았어도, 본문의 '갤럭시 S25'는 인용이 아니다
        evidences = [hit(f"S{i}", f"c{i}") for i in range(1, 26)]
        result = loop_result("갤럭시 S25 판매 호조로 매출 증가 [S1].", ["S1"], evidences)
        finding = merge_loop_results(subtopic(), 1, [("q", result, None)], self.registry)
        self.assertEqual(finding.summary, "갤럭시 S25 판매 호조로 매출 증가 [S1].")
        self.assertEqual(finding.evidence_ids, ["S1"])
        self.assertEqual(len(self.registry), 1)

    def test_evidence_without_location_is_excluded_not_fatal(self):
        bad = {"evidence_id": "S2", "source_file": XML, "chunk_id": "x", "text": "본문", "pdf_page": None}
        result = loop_result("A [S1] B [S2]", ["S1", "S2"], [hit("S1", "is-1"), bad])
        finding = merge_loop_results(subtopic(), 1, [("q", result, None)], self.registry)
        self.assertEqual(finding.summary, "A [S1] B")
        self.assertEqual(finding.evidence_ids, ["S1"])
        self.assertTrue(any("위치" in note for note in finding.limitations))

    def test_errors_and_insufficient_answers_unchanged(self):
        insufficient = {"findings": {"status": "insufficient_evidence", "limitations": ["근거 없음"]}, "evidences": []}
        finding = merge_loop_results(subtopic(), 1, [("q1", None, "조사 실패"), ("q2", insufficient, None)],
                                     self.registry)
        self.assertEqual(finding.status, "insufficient_evidence")
        self.assertEqual(len(finding.limitations), 2)


class TestMergeWithoutRegistry(unittest.TestCase):
    def test_falls_back_to_prefixed_ids(self):
        result = loop_result("A [S1]", ["S1"], [hit("S1", "is-1")])
        finding = merge_loop_results(subtopic(), 2, [("q", result, None)])
        self.assertEqual(finding.summary, "A [profitability.r2.q1.S1]")
        self.assertEqual(finding.evidence_ids, ["profitability.r2.q1.S1"])


class TestLoopResearcherUsesConfigRegistry(unittest.TestCase):
    def test_registry_from_config(self):
        def fake_loop(question, *_, **__):
            return loop_result(f"{question} 결과 [S1]", ["S1"], [hit("S1", "is-1")])

        class FakeTools:
            persist_directory = Path(".")

            def search_function(self, target):
                return lambda q, k: []

        researcher = make_loop_researcher(search_tools=FakeTools(), questions_per_subtopic=2, loop_fn=fake_loop)
        brief = Brief(company="삼성전자", corp_code="00126380", years=[2025], objective="분석",
                      subtopics=[subtopic()])
        registry = EvidenceRegistry()
        out = asyncio.run(researcher({"subtopic": subtopic(), "brief": brief, "round": 1},
                                     {"configurable": {"evidence_registry": registry}}))
        finding = out["findings"][0]
        self.assertEqual(finding.summary, "질문1 결과 [S1]\n질문2 결과 [S1]")
        self.assertEqual(len(registry), 1)


if __name__ == "__main__":
    unittest.main()
