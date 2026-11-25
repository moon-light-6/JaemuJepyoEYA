
"""XML draft 평가 데이터셋 로더 단위 테스트."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


# 프로젝트 경로 설정
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.retrieval.xml_draft_evaluation import (
    load_xml_draft_eval_dataset,
)


class XMLDraftEvaluationTest(unittest.TestCase):

    def setUp(self):
        """각 테스트에서 사용할 임시 평가 JSON 파일을 생성한다."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dataset_path = Path(self.temp_dir.name) / "retrieval.json"

    def tearDown(self):
        """테스트 종료 후 임시 파일을 정리한다."""
        self.temp_dir.cleanup()

    def create_draft_item(self, **overrides):
        """테스트용 정상 draft 질문을 생성한다."""
        item = {
            "id": "X01",
            "question": "2025년 연결 기준 자산총계는 얼마인가?",
            "answer_type": "numeric",
            "expected_value": "100,000",
            "unit": "백만원",
            "gold_sources": [
                {
                    "source_file": "original.xml",
                    "source_xpath": "/OLD/PATH",
                }
            ],
            "candidate_sources": [
                {
                    "source_file": "report.xml",
                    "source_xpath": "/DOCUMENT[1]/TABLE[5]",
                    "title": "재무상태표",
                    "match_reason": "계정명과 당기 금액이 같은 XML 문서",
                }
            ],
            "annotation_status": "draft",
        }
        item.update(overrides)
        return item

    def save_dataset(self, dataset):
        """테스트 데이터를 JSON 파일로 저장한다."""
        self.dataset_path.write_text(
            json.dumps(dataset, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 1. 정상적인 draft 질문 로딩
    def test_load_valid_draft_dataset(self):
        item = self.create_draft_item()
        self.save_dataset([item])

        questions = load_xml_draft_eval_dataset(self.dataset_path)

        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["id"], "X01")
        self.assertEqual(
            questions[0]["question"],
            "2025년 연결 기준 자산총계는 얼마인가?",
        )
        self.assertEqual(questions[0]["evaluation_mode"], "draft")
        self.assertEqual(
            questions[0]["reference_type"],
            "candidate_sources",
        )

    # 2. candidate_sources를 gold_sources에 연결
    def test_candidate_sources_are_used_as_evaluation_sources(self):
        item = self.create_draft_item()
        self.save_dataset([item])

        questions = load_xml_draft_eval_dataset(self.dataset_path)

        expected_sources = [
            {
                "source_file": "report.xml",
                "source_xpath": "/DOCUMENT[1]/TABLE[5]",
            }
        ]

        self.assertEqual(
            questions[0]["gold_sources"],
            expected_sources,
        )

    # 3. 원본 gold_sources를 사용하지 않는지 확인
    def test_original_gold_sources_are_not_used(self):
        item = self.create_draft_item()
        self.save_dataset([item])

        questions = load_xml_draft_eval_dataset(self.dataset_path)

        self.assertNotEqual(
            questions[0]["gold_sources"][0]["source_file"],
            "original.xml",
        )

    # 4. draft와 verified가 섞여 있으면 draft만 로딩
    def test_load_only_draft_questions(self):
        draft = self.create_draft_item(id="X01")

        verified = self.create_draft_item(
            id="X02",
            annotation_status="verified",
        )

        self.save_dataset([draft, verified])

        questions = load_xml_draft_eval_dataset(self.dataset_path)

        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["id"], "X01")

    # 5. verified 질문만 존재하는 경우
    def test_no_draft_questions_raises_error(self):
        verified = self.create_draft_item(
            annotation_status="verified",
        )
        self.save_dataset([verified])

        with self.assertRaisesRegex(ValueError, "draft 질문이 없습니다"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 6. 중복 질문 ID 차단
    def test_duplicate_question_id_raises_error(self):
        item1 = self.create_draft_item(id="X01")
        item2 = self.create_draft_item(id="X01")

        self.save_dataset([item1, item2])

        with self.assertRaisesRegex(ValueError, "중복된 XML draft 평가 질문 ID"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 7. 질문 내용 누락
    def test_empty_question_raises_error(self):
        item = self.create_draft_item(question=" ")
        self.save_dataset([item])

        with self.assertRaisesRegex(ValueError, "질문 내용이 비어 있습니다"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 8. expected_value 누락
    def test_missing_expected_value_raises_error(self):
        item = self.create_draft_item(expected_value=None)
        self.save_dataset([item])

        with self.assertRaisesRegex(ValueError, "expected_value"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 9. 비교형 expected_value 지원
    def test_comparison_expected_value(self):
        item = self.create_draft_item(
            answer_type="comparison",
            expected_value={
                "2025": "100,000",
                "2024": "90,000",
            },
        )
        self.save_dataset([item])

        questions = load_xml_draft_eval_dataset(self.dataset_path)

        self.assertEqual(
            questions[0]["expected_value"]["2025"],
            "100,000",
        )

    # 10. candidate_sources 누락
    def test_missing_candidate_sources_raises_error(self):
        item = self.create_draft_item(candidate_sources=[])
        self.save_dataset([item])

        with self.assertRaisesRegex(ValueError, "candidate_sources"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 11. source_file 누락
    def test_missing_source_file_raises_error(self):
        item = self.create_draft_item(
            candidate_sources=[
                {
                    "source_xpath": "/DOCUMENT[1]/TABLE[5]",
                }
            ]
        )
        self.save_dataset([item])

        with self.assertRaisesRegex(ValueError, "source_file"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 12. source_xpath 누락
    def test_missing_source_xpath_raises_error(self):
        item = self.create_draft_item(
            candidate_sources=[
                {
                    "source_file": "report.xml",
                }
            ]
        )
        self.save_dataset([item])

        with self.assertRaisesRegex(ValueError, "source_file과 source_xpath"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 13. 중복된 후보 근거 제거
    def test_duplicate_candidate_sources_are_removed(self):
        source = {
            "source_file": "report.xml",
            "source_xpath": "/DOCUMENT[1]/TABLE[5]",
        }

        item = self.create_draft_item(
            candidate_sources=[source, source]
        )
        self.save_dataset([item])

        questions = load_xml_draft_eval_dataset(self.dataset_path)

        self.assertEqual(
            len(questions[0]["gold_sources"]),
            1,
        )

    # 14. 지원하지 않는 annotation_status
    def test_invalid_annotation_status_raises_error(self):
        item = self.create_draft_item(
            annotation_status="pending"
        )
        self.save_dataset([item])

        with self.assertRaisesRegex(ValueError, "annotation_status"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 15. 빈 JSON 배열
    def test_empty_dataset_raises_error(self):
        self.save_dataset([])

        with self.assertRaisesRegex(ValueError, "JSON 배열"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 16. 잘못된 JSON 형식
    def test_invalid_json_raises_error(self):
        self.dataset_path.write_text(
            "{invalid json}",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "JSON 형식 오류"):
            load_xml_draft_eval_dataset(self.dataset_path)

    # 17. 파일이 존재하지 않는 경우
    def test_missing_dataset_file_raises_error(self):
        missing_path = Path(self.temp_dir.name) / "missing.json"

        with self.assertRaises(FileNotFoundError):
            load_xml_draft_eval_dataset(missing_path)


if __name__ == "__main__":
    unittest.main()
