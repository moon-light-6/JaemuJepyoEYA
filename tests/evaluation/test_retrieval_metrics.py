"""검색 평가셋 로드 및 Hit@K 계산의 기본 단위 테스트."""

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.retrieval_metrics import (
    load_eval_dataset,
    is_hit,
    calculate_hit_rate,
)


class RetrievalMetricsTest(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dataset_path = Path(self.temp_dir.name) / "eval.json"

        self.dataset = [
            {
                "id": "S01",
                "question": "매출액은?",
                "gold_sources": [
                    {"source_file": "test.pdf", "pdf_page": 85}
                ],
                "annotation_status": "verified",
            },
            {
                "id": "S02",
                "question": "자산총계는?",
                "gold_sources": [
                    {"source_file": "test.pdf", "pdf_page": 83}
                ],
                "annotation_status": "verified",
            },
        ]

        self.dataset_path.write_text(
            json.dumps(self.dataset, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    # 평가 질문을 정상적으로 읽어오는지
    def test_load_dataset(self):
        questions = load_eval_dataset(self.dataset_path)
        self.assertEqual(len(questions), 2)

    # Hit@K 판정
    def test_hit(self):
        results = [
            {"source_file": "test.pdf", "pdf_page": 83},
            {"source_file": "test.pdf", "pdf_page": 85},
        ]

        self.assertTrue(is_hit(results, self.dataset[0]["gold_sources"], 3))
        self.assertFalse(is_hit(results, self.dataset[0]["gold_sources"], 1))

    # 출처 파일명까지 비교
    def test_source_file_must_match(self):
        results = [
            {"source_file": "other.pdf", "pdf_page": 85}
        ]

        self.assertFalse(is_hit(results, self.dataset[0]["gold_sources"], 3))

    # 정답 페이지가 여러 개인 경우
    def test_multiple_gold_pages(self):
        gold_sources = [
            {"source_file": "test.pdf", "pdf_page": 129},
            {"source_file": "test.pdf", "pdf_page": 130},
        ]
        results = [
            {"source_file": "test.pdf", "pdf_page": 130}
        ]

        self.assertTrue(is_hit(results, gold_sources, 3))

    # 전체 성공률 계산
    def test_hit_rate(self):
        evaluations = [
            {
                "results": [{"source_file": "test.pdf", "pdf_page": 85}],
                "gold_sources": self.dataset[0]["gold_sources"],
            },
            {
                "results": [{"source_file": "test.pdf", "pdf_page": 90}],
                "gold_sources": self.dataset[1]["gold_sources"],
            },
        ]

        self.assertEqual(calculate_hit_rate(evaluations, 3), 0.5)

    # k=0처럼 잘못된 값을 거부하는지
    def test_invalid_k(self):
        with self.assertRaises(ValueError):
            is_hit([], self.dataset[0]["gold_sources"], 0)

    # draft를 제외하고 verified만 평가하는지
    def test_only_verified(self):
        self.dataset[1]["annotation_status"] = "draft"

        self.dataset_path.write_text(
            json.dumps(self.dataset, ensure_ascii=False),
            encoding="utf-8",
        )

        questions = load_eval_dataset(self.dataset_path)
        self.assertEqual(len(questions), 1)


if __name__ == "__main__":
    unittest.main()
