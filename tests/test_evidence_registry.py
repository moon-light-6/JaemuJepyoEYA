"""EvidenceRegistry: 전역 S# 발급·중복 제거·조회와 근거 형식 검사를 파일 없이 검증합니다.

scripts/check_evidence_registry.py는 실제 XML v2 청크 파일이 있어야 돌아가므로,
여기서는 가짜 청크로 같은 규칙을 단위 테스트로 고정한다.
"""

import sys
import threading
import unittest
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fin_research.evidence.models import Evidence, EvidenceCandidate
from fin_research.evidence.registry import EvidenceRegistry

XML = "20260313001021_00761.xml"


def chunk(chunk_id, xpath="/DOCUMENT[1]/BODY[1]/TABLE[1]", text="영업이익 | 2,208,138", **kw):
    return {"source_file": XML, "chunk_id": chunk_id, "source_xpath": xpath, "text": text, **kw}


class TestRegister(unittest.TestCase):
    def setUp(self):
        self.registry = EvidenceRegistry()

    def test_numbers_in_order(self):
        self.assertEqual([self.registry.register(chunk(c)) for c in ("a", "b", "c")], ["S1", "S2", "S3"])
        self.assertEqual(len(self.registry), 3)

    def test_same_chunk_returns_same_id(self):
        first = self.registry.register(chunk("a"))
        self.assertEqual(self.registry.register(chunk("a", text="다른 검색어로 다시 찾음")), first)
        self.assertEqual(len(self.registry), 1)

    def test_same_xpath_different_chunk_gets_new_id(self):
        # 긴 표 하나가 여러 청크로 나뉜 경우: XPath가 같아도 다른 근거
        ids = {self.registry.register(chunk(c, xpath="/DOCUMENT[1]/TABLE[3]")) for c in ("t3-1", "t3-2")}
        self.assertEqual(ids, {"S1", "S2"})

    def test_same_chunk_id_in_different_files_is_different(self):
        a = self.registry.register(chunk("c1"))
        b = self.registry.register({**chunk("c1"), "source_file": "20260313001021_00760.xml"})
        self.assertNotEqual(a, b)

    def test_accepts_search_result_dict_with_extra_keys(self):
        # 검색기 결과(score, metadata 등)를 그대로 넘겨도 된다
        evidence_id = self.registry.register({**chunk("a"), "score": 0.83, "metadata": {"x": 1}})
        self.assertEqual(self.registry.require(evidence_id).chunk_id, "a")

    def test_accepts_pdf_page_instead_of_xpath(self):
        evidence_id = self.registry.register({"source_file": "report.pdf", "chunk_id": "p84-1",
                                              "text": "자본총계 363조", "pdf_page": 84})
        self.assertEqual(self.registry.require(evidence_id).pdf_page, 84)


class TestLookup(unittest.TestCase):
    def setUp(self):
        self.registry = EvidenceRegistry()
        self.registry.register(chunk("a", title="연결 손익계산서"))
        self.registry.register(chunk("b", xpath="/DOCUMENT[1]/TABLE[2]", text="자산총계 | 41,084,496"))

    def test_require_returns_original(self):
        evidence = self.registry.require("S2")
        self.assertIsInstance(evidence, Evidence)
        self.assertEqual((evidence.chunk_id, evidence.source_xpath, evidence.text),
                         ("b", "/DOCUMENT[1]/TABLE[2]", "자산총계 | 41,084,496"))

    def test_get_and_require_unknown(self):
        self.assertIsNone(self.registry.get("S99"))
        with self.assertRaises(KeyError):
            self.registry.require("S99")

    def test_values_in_issue_order(self):
        self.assertEqual([e.evidence_id for e in self.registry.values()], ["S1", "S2"])
        self.assertEqual(self.registry.values()[0].title, "연결 손익계산서")


class TestCandidateValidation(unittest.TestCase):
    def test_location_required(self):
        with self.assertRaises(ValidationError):
            EvidenceCandidate(source_file=XML, chunk_id="a", text="본문")
        with self.assertRaises(ValidationError):  # 공백 XPath는 없는 것과 같다
            EvidenceCandidate(source_file=XML, chunk_id="a", text="본문", source_xpath="   ")

    def test_empty_text_rejected(self):
        for field in ("source_file", "chunk_id", "text"):
            with self.assertRaises(ValidationError, msg=field):
                EvidenceCandidate(**{**chunk("a"), field: "  "})

    def test_invalid_page_rejected(self):
        with self.assertRaises(ValidationError):
            EvidenceCandidate(source_file="r.pdf", chunk_id="a", text="본문", pdf_page=0)

    def test_evidence_id_format(self):
        for bad in ("S0", "s1", "S01", "1", "S1a"):
            with self.assertRaises(ValidationError, msg=bad):
                Evidence(evidence_id=bad, **chunk("a"))

    def test_invalid_registration_does_not_consume_number(self):
        registry = EvidenceRegistry()
        with self.assertRaises(ValidationError):
            registry.register({"source_file": XML, "chunk_id": "x", "text": "위치 없음"})
        self.assertEqual(registry.register(chunk("a")), "S1")


class TestConcurrency(unittest.TestCase):
    def test_parallel_researchers_get_unique_ids(self):
        # researcher 8개가 같은 청크 50개를 동시에 등록 → 번호는 S1~S50, 겹치거나 빠지지 않는다
        registry = EvidenceRegistry()
        results: list[list[str]] = []
        lock = threading.Lock()

        def worker(offset: int):
            ids = [registry.register(chunk(f"c{(i + offset) % 50}")) for i in range(50)]
            with lock:
                results.append(ids)

        threads = [threading.Thread(target=worker, args=(n * 7,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(registry), 50)
        self.assertEqual({e.evidence_id for e in registry.values()}, {f"S{i}" for i in range(1, 51)})
        by_chunk = {e.chunk_id: e.evidence_id for e in registry.values()}
        for ids in results:  # 모든 스레드가 같은 청크에 같은 번호를 받았다
            for i, evidence_id in enumerate(ids):
                self.assertEqual(by_chunk[registry.require(evidence_id).chunk_id], evidence_id)


if __name__ == "__main__":
    unittest.main()