"""6가지 평가 유형과 잘못된 실행 결과를 탐지하는 채점기를 검증한다."""

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evaluation.loop_fixture_runner import run_fixture_case
from evaluation.loop_qa import CATEGORY_LABELS, LoopObservation, LoopQACase, load_jsonl
from evaluation.metrics.loop_metrics import evaluate_case, summarize
from evaluation.run_eval import evaluate_observations


class LoopQATests(unittest.TestCase):
    """실제 모델·DB 접속 없이 평가 계약과 기존 구성요소의 연결을 확인한다."""

    @classmethod
    def setUpClass(cls):
        """기본 평가셋과 정답을 참조하지 않는 실행 결과를 한 번 준비한다."""
        cls.cases = load_jsonl(ROOT / "evaluation/dataset/loop_qa.jsonl", LoopQACase)
        cls.by_id = {case.id: case for case in cls.cases}
        cls.observations = {case.id: run_fixture_case(case) for case in cls.cases}

    def grade(self, identifier, **updates):
        """관측값 일부를 의도적으로 바꿔 채점기가 오류를 탐지하는지 확인한다."""
        raw = self.observations[identifier].model_dump()
        raw.update(updates)
        return evaluate_case(self.by_id[identifier], LoopObservation.model_validate(raw))

    def test_default_dataset_covers_all_six_categories(self):
        """배포한 기본 질문이 요청한 6가지 유형을 모두 포함하는지 검사한다."""
        self.assertEqual({case.category for case in self.cases}, set(CATEGORY_LABELS))
        self.assertEqual(len(self.cases), 13)
        self.assertTrue(all(case.data_origin == "synthetic_fixture" for case in self.cases))

    def test_fixture_components_satisfy_declared_contracts(self):
        """모의 도구로 실행한 13개 사례를 정답 계약과 독립적으로 비교한다."""
        report = evaluate_observations(self.cases, list(self.observations.values()))
        self.assertEqual(report["summary"]["passed"], len(self.cases))
        self.assertEqual(report["summary"]["failure_types"], {})
        self.assertEqual(self.observations["LQ03"].numbers[0].value, Decimal("20"))

    def test_numeric_value_and_unit_are_checked_separately(self):
        """숫자 또는 단위가 틀리면 다른 항목이 맞더라도 통과하지 못하게 한다."""
        for field, value, failure in (("value", "999999", "numeric_value"), ("unit", "백만원", "numeric_unit")):
            raw = self.observations["LQ01"].model_dump()
            raw["numbers"][0][field] = value
            result = evaluate_case(self.by_id["LQ01"], LoopObservation.model_validate(raw))
            self.assertIn(failure, result.failure_types)

    def test_same_number_with_wrong_year_or_scope_fails(self):
        """숫자가 같아도 실적 연도나 연결·별도가 다르면 오류로 분류한다."""
        for field, value in (("year", 2024), ("scope", "separate")):
            raw = self.observations["LQ01"].model_dump()
            raw["numbers"][0]["context"][field] = value
            result = evaluate_case(self.by_id["LQ01"], LoopObservation.model_validate(raw))
            self.assertIn("context_consistency", result.failure_types)

    def test_unknown_citation_is_rejected(self):
        """검색·조회하지 않은 출처 ID로 답변을 꾸민 경우를 탐지한다."""
        result = self.grade("LQ02", cited_source_ids=["invented-source"])
        self.assertIn("citation_integrity", result.failure_types)
        self.assertIn("required_evidence", result.failure_types)

    def test_numeric_provenance_is_required(self):
        """금액이 맞아도 숫자의 근거가 답변 인용과 연결되지 않으면 실패시킨다."""
        raw = self.observations["LQ01"].model_dump()
        raw["numbers"][0]["source_ids"] = ["fabricated-sql-row"]
        result = evaluate_case(self.by_id["LQ01"], LoopObservation.model_validate(raw))
        self.assertIn("numeric_provenance", result.failure_types)

    def test_combined_question_requires_both_tools_and_text(self):
        """숫자와 설명을 요구한 질문에서 한쪽 작업을 생략한 결과를 탐지한다."""
        result = self.grade("LQ04", answer="매출액만 조회했습니다.",
                            tool_trace=[self.observations["LQ04"].tool_trace[0]])
        self.assertIn("tool_sequence", result.failure_types)
        self.assertIn("text_coverage", result.failure_types)

    def test_missing_data_is_not_accepted_as_zero(self):
        """빈 조회를 금액 0으로 확정하는 결과를 올바른 보류로 인정하지 않는다."""
        result = self.grade("LQ05", status="answered", answer="매출은 0원입니다.")
        self.assertIn("answer_status", result.failure_types)
        self.assertIn("answer_grounding", result.failure_types)

    def test_loop_stopping_checks_reason_and_search_count(self):
        """중복 근거 종료 사례에서 불필요한 반복이나 잘못된 종료 코드를 탐지한다."""
        result = self.grade("LQ07", rounds=4, stop_reason="sufficient_evidence")
        self.assertIn("round_bounds", result.failure_types)
        self.assertIn("stop_reason", result.failure_types)
        self.assertEqual(self.observations["LQ06"].rounds, 2)
        self.assertEqual(self.observations["LQ08"].rounds, 1)

    def test_partial_answer_keeps_checked_number_and_exposes_missing_explanation(self):
        """부분 답변도 금액·출처를 검수하고 미확보 설명을 완료로 표시하지 못하게 한다."""
        actual = self.observations["LQ13"]
        self.assertEqual(actual.status, "partial")
        self.assertEqual(actual.numbers[0].value, Decimal("1000000"))
        self.assertIn("partial_safety", self.grade("LQ13", limitations=[]).failure_types)
        self.assertIn("component_status", self.grade("LQ13", component_status={}).failure_types)

    def test_abstention_requires_an_explanation(self):
        """보류 상태만 표시하고 이유를 비워 둔 결과를 실패시킨다."""
        self.assertIn("abstention_safety", self.grade("LQ10", limitations=[]).failure_types)

    def test_empty_first_search_can_recover_with_new_evidence(self):
        """첫 검색이 비어도 다음 검색이 성공하면 미해결 실패로 오판하지 않는다."""
        raw = self.by_id["LQ02"].model_dump()
        raw["fixture"]["retrieval_rounds"] = [[], ["doc-demand"]]
        raw["fixture"]["decisions"].insert(0, {
            "sufficient": False, "source_ids": [], "reason": "첫 검색 결과가 없다.",
            "next_query": "다른 검색어로 매출 원인 찾기",
        })
        raw["expected"].update(tools=["doc_search", "doc_search"], min_rounds=2, max_rounds=2)
        case = LoopQACase.model_validate(raw)
        actual = run_fixture_case(case)
        self.assertTrue(evaluate_case(case, actual).passed)
        self.assertEqual([event.status for event in actual.tool_trace], ["empty", "ok"])

    def test_missing_result_is_counted_and_unknown_result_is_rejected(self):
        """실패한 질문을 결과 파일에서 빼 통과율을 높이지 못하도록 한다."""
        report = evaluate_observations(self.cases, list(self.observations.values())[:-1])
        self.assertEqual(report["summary"]["total"], 13)
        self.assertEqual(report["summary"]["passed"], 12)
        self.assertEqual(report["summary"]["failure_types"]["missing_prediction"], 1)
        unknown = self.observations["LQ01"].model_copy(update={"case_id": "unknown"})
        with self.assertRaises(ValueError):
            evaluate_observations(self.cases, [unknown])

    def test_jsonl_loader_rejects_duplicate_ids_and_invalid_sources(self):
        """중복 질문과 존재하지 않는 모의 문서를 참조하는 평가셋을 거부한다."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "questions.jsonl"
            line = self.cases[0].model_dump_json()
            path.write_text(line + "\n" + line + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "중복"):
                load_jsonl(path, LoopQACase)
        raw = self.by_id["LQ02"].model_dump()
        raw["fixture"]["retrieval_rounds"] = [["missing-document"]]
        with self.assertRaises(ValueError):
            LoopQACase.model_validate(raw)

    def test_unevaluated_category_has_no_invented_score(self):
        """일부 유형만 평가할 때 나머지를 임의의 성공·실패 점수로 표시하지 않는다."""
        result = evaluate_case(self.by_id["LQ01"], self.observations["LQ01"])
        self.assertIsNone(summarize([result])["categories"]["document_lookup"]["pass_rate"])


if __name__ == "__main__":
    unittest.main()
