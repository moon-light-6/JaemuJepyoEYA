"""수치·단위·출처·도구·보류·종료를 별도 채점하는 루프 평가 지표."""

from collections import Counter
from decimal import Context, localcontext

from evaluation.loop_qa import (
    CATEGORY_LABELS, LoopObservation, LoopQACase, StrictModel,
)


class CheckResult(StrictModel):
    """개별 평가 기준의 통과 여부와 검수 가능한 설명을 저장한다."""

    name: str
    passed: bool
    detail: str


class CaseEvaluation(StrictModel):
    """질문 하나의 평가 결과와 실패 유형을 보관한다."""

    case_id: str
    category: str
    passed: bool
    failure_types: list[str]
    checks: list[CheckResult]


def evaluate_case(case: LoopQACase, actual: LoopObservation | None) -> CaseEvaluation:
    """정답을 실행 출력과 비교하며 결과가 없을 때도 실패로 집계한다."""
    checks = []

    def check(name: str, passed: bool, detail: str) -> None:
        """평가 기준의 이름·판정·설명을 일관된 형식으로 추가한다."""
        checks.append(CheckResult(name=name, passed=passed, detail=detail))

    if actual is None:
        check("missing_prediction", False, "이 질문의 실행 결과가 없습니다.")
    else:
        check("case_identity", actual.case_id == case.id, "질문 ID가 일치해야 합니다.")
        check("answer_status", actual.status == case.expected.status,
              f"기대={case.expected.status}, 실제={actual.status}")
        tools = [event.tool for event in actual.tool_trace]
        check("tool_sequence", tools == case.expected.tools,
              f"기대={case.expected.tools}, 실제={tools}")
        expected = {number.metric: number for number in case.expected.numbers}
        numbers = {number.metric: number for number in actual.numbers}
        check("numeric_metrics", len(numbers) == len(actual.numbers) and set(numbers) == set(expected),
              f"기대 지표={sorted(expected)}, 실제 지표={sorted(numbers)}")
        for metric, target in expected.items():
            number = numbers.get(metric)
            with localcontext(Context(prec=50)):
                matched = number is not None and abs(number.value - target.value) <= target.tolerance
            check("numeric_value", matched, f"{metric}: 정답={target.value}, 허용오차={target.tolerance}")
            check("numeric_unit", number is not None and number.unit == target.unit,
                  f"{metric}: 기대 단위={target.unit}")
        check("text_coverage", all(phrase in actual.answer for phrase in case.expected.required_phrases),
              "지정한 설명 문구가 답변에 포함되는지 확인합니다. 의미·인과관계 전체 검증은 아닙니다.")
        evidence_ids = {evidence.id for evidence in actual.evidences}
        cited = set(actual.cited_source_ids)
        check("evidence_identity", len(evidence_ids) == len(actual.evidences), "근거 ID는 중복될 수 없습니다.")
        check("citation_integrity", cited <= evidence_ids, "인용한 출처는 확보한 근거에 실제로 있어야 합니다.")
        check("required_evidence", set(case.expected.required_source_ids) <= cited,
              f"필수 출처={case.expected.required_source_ids}, 실제 인용={sorted(cited)}")
        if actual.status == "answered":
            check("answer_grounding", bool(actual.answer.strip()) and bool(cited),
                  "확정 답변에는 내용과 출처가 있어야 합니다.")
            last_outcomes = {event.tool: event.status for event in actual.tool_trace}
            check("tool_outcomes", all(status == "ok" for status in last_outcomes.values()),
                  "재시도로 복구한 실패는 허용하되 마지막 실행에 미해결 오류·차단·빈 조회가 남으면 실패합니다.")
        else:
            check("abstention_safety", not actual.numbers and bool(actual.limitations),
                  "보류·오류 시 확정 숫자를 내놓지 않고 이유를 기록해야 합니다.")
        for number in actual.numbers:
            check("numeric_provenance", bool(number.source_ids) and set(number.source_ids) <= cited,
                  f"{number.metric}: 숫자의 출처가 확보·인용된 근거여야 합니다.")
        check("context_consistency",
              all(number.context == case.context for number in actual.numbers)
              and all(evidence.context == case.context for evidence in actual.evidences if evidence.id in cited),
              "인용한 근거와 숫자의 회사·실적 연도·연결/별도·통화가 질문과 일치해야 합니다.")
        check("stop_reason", actual.stop_reason in case.expected.stop_reasons,
              f"기대={case.expected.stop_reasons}, 실제={actual.stop_reason}")
        check("round_bounds", case.expected.min_rounds <= actual.rounds <= case.expected.max_rounds,
              f"검색 횟수={actual.rounds}, 허용={case.expected.min_rounds}~{case.expected.max_rounds}")
    failures = sorted({result.name for result in checks if not result.passed})
    return CaseEvaluation(case_id=case.id, category=case.category,
                          passed=not failures, failure_types=failures, checks=checks)


def summarize(results: list[CaseEvaluation]) -> dict:
    """6가지 유형별 통과 수와 실패 원인을 집계한다. 없는 유형은 0점 대신 미평가로 표시한다."""
    categories = {}
    for category, label in CATEGORY_LABELS.items():
        selected = [result for result in results if result.category == category]
        passed = sum(result.passed for result in selected)
        categories[category] = {"label": label, "total": len(selected), "passed": passed,
                                "pass_rate": passed / len(selected) if selected else None}
    return {
        "total": len(results), "passed": sum(result.passed for result in results),
        "pass_rate": sum(result.passed for result in results) / len(results) if results else None,
        "categories": categories,
        "failure_types": dict(Counter(name for result in results for name in result.failure_types)),
    }
