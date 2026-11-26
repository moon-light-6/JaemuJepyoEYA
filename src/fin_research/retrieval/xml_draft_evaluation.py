
"""XML draft 평가 데이터셋 로더.

자동 생성한 draft 평가 JSON을 읽어 검색 평가에 사용할
질문 목록으로 변환한다.

기존 verified 평가 로더(load_xml_eval_dataset)는 수정하지 않는다.

주의:
    draft의 candidate_sources는 사람이 검증한 정답 근거가 아니다.
    따라서 이 로더를 사용한 검색 평가 결과는 공식 검색 성능이 아닌
    후보 근거 기반의 임시 평가 결과로 구분해야 한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def validate_candidate_sources(
    question_id: str,
    candidate_sources: Any,
) -> list[dict[str, str]]:
    """XML 후보 근거의 source_file과 source_xpath를 검증한다.

    Args:
        question_id: 평가 질문 ID (예: X01)
        candidate_sources: 자동 생성된 후보 근거 목록

    Returns:
        검색 평가에 필요한 출처 정보 목록

    Raises:
        ValueError: 후보 근거가 없거나 출처 형식이 잘못된 경우
    """

    if not isinstance(candidate_sources, list) or not candidate_sources:
        raise ValueError(
            f"{question_id}: XML candidate_sources가 없습니다."
        )

    sources: list[dict[str, str]] = []
    seen_sources: set[tuple[str, str]] = set()

    for index, source in enumerate(candidate_sources, start=1):
        if not isinstance(source, dict):
            raise ValueError(
                f"{question_id}: {index}번째 후보 근거가 JSON 객체가 아닙니다."
            )

        source_file = source.get("source_file")
        source_xpath = source.get("source_xpath")

        if (
            not isinstance(source_file, str)
            or not source_file.strip()
            or not isinstance(source_xpath, str)
            or not source_xpath.strip()
        ):
            raise ValueError(
                f"{question_id}: 후보 근거에는 유효한 "
                "source_file과 source_xpath가 필요합니다."
            )

        source_file = source_file.strip()
        source_xpath = source_xpath.strip()

        source_key = (source_file, source_xpath)

        # 동일한 XML 위치가 중복 등록된 경우 하나만 사용한다.
        if source_key in seen_sources:
            continue

        seen_sources.add(source_key)

        sources.append(
            {
                "source_file": source_file,
                "source_xpath": source_xpath,
            }
        )

    return sources


def validate_expected_value(
    question_id: str,
    expected_value: Any,
) -> None:
    """평가 질문의 기대값을 검사한다.

    기존 XML 평가셋에서 사용하는 두 가지 형식을 지원한다.

    numeric:
        "30,107,783"

    comparison:
        {"2025": "30,107,783", "2024": "30,515,255"}
    """

    if isinstance(expected_value, str):
        if expected_value.strip():
            return

    elif isinstance(expected_value, dict) and expected_value:
        if all(
            isinstance(value, str) and value.strip()
            for value in expected_value.values()
        ):
            return

    raise ValueError(
        f"{question_id}: expected_value가 없거나 형식이 잘못되었습니다."
    )


def load_xml_draft_eval_dataset(
    dataset_path: str | Path,
) -> list[dict[str, Any]]:
    """XML 평가 JSON에서 draft 질문만 로딩한다.

    처리 과정:
        1. 평가 JSON 파일 읽기
        2. JSON 배열 형식 검사
        3. annotation_status가 draft인 질문 선택
        4. 질문 ID, 질문 내용, 기대값 검사
        5. candidate_sources 검사
        6. 기존 evaluate_xml()과 호환되는 데이터로 변환

    Args:
        dataset_path: 승연님이 생성한 retrieval.json 경로

    Returns:
        검색 평가에 사용할 draft 질문 목록

    Raises:
        FileNotFoundError: 평가셋 파일이 없는 경우
        ValueError: 데이터 형식이 잘못되었거나 draft 질문이 없는 경우
    """

    dataset_path = Path(dataset_path)

    # 1. 파일 존재 여부 확인
    if not dataset_path.is_file():
        raise FileNotFoundError(
            f"XML 평가셋 파일을 찾을 수 없습니다: {dataset_path}"
        )

    # 2. JSON 파일 로딩
    try:
        dataset = json.loads(
            dataset_path.read_text(encoding="utf-8")
        )

    except json.JSONDecodeError as error:
        raise ValueError(
            f"XML 평가셋 JSON 형식 오류: {dataset_path} "
            f"(line {error.lineno}, column {error.colno})"
        ) from error

    # 3. 데이터셋 형식 확인
    if not isinstance(dataset, list) or not dataset:
        raise ValueError(
            "XML 평가셋은 비어 있지 않은 JSON 배열이어야 합니다."
        )

    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # 4. 질문별 검증
    for index, item in enumerate(dataset, start=1):
        if not isinstance(item, dict):
            raise ValueError(
                f"{index}번째 XML 평가 질문은 JSON 객체여야 합니다."
            )

        status = item.get("annotation_status")

        # 지원하지 않는 상태는 명시적인 오류로 처리한다.
        if status not in ("draft", "verified"):
            raise ValueError(
                f"{index}번째 질문의 annotation_status가 "
                "draft 또는 verified가 아닙니다."
            )

        # verified 질문은 기존 평가 파이프라인에서 처리한다.
        if status != "draft":
            continue

        question_id = item.get("id")
        question = item.get("question")
        expected_value = item.get("expected_value")
        candidate_sources = item.get("candidate_sources")

        # 질문 ID 확인
        if not isinstance(question_id, str) or not question_id.strip():
            raise ValueError(
                f"{index}번째 draft 질문의 ID가 올바르지 않습니다."
            )

        question_id = question_id.strip()

        # 질문 ID 중복 확인
        if question_id in seen_ids:
            raise ValueError(
                f"중복된 XML draft 평가 질문 ID: {question_id}"
            )

        # 질문 내용 확인
        if not isinstance(question, str) or not question.strip():
            raise ValueError(
                f"{question_id}: 질문 내용이 비어 있습니다."
            )

        # 기대값 확인
        validate_expected_value(
            question_id,
            expected_value,
        )

        # 후보 근거 확인
        sources = validate_candidate_sources(
            question_id,
            candidate_sources,
        )

        # 5. 기존 검색 평가 함수에 전달할 데이터 생성
        #
        # 기존 evaluate_xml()은 gold_sources 필드를 사용한다.
        # draft에서는 검증되지 않은 gold_sources 대신
        # candidate_sources를 임시 평가 기준으로 전달한다.
        #
        # 원본 JSON 파일은 변경하지 않는다.
        adapted_item = dict(item)

        adapted_item["id"] = question_id
        adapted_item["question"] = question.strip()
        adapted_item["gold_sources"] = sources

        # 평가 결과에서 draft 여부를 구분하기 위한 정보
        adapted_item["evaluation_mode"] = "draft"
        adapted_item["reference_type"] = "candidate_sources"

        questions.append(adapted_item)
        seen_ids.add(question_id)

    # 6. 실제 평가할 질문이 없는 경우
    if not questions:
        raise ValueError(
            "평가할 XML draft 질문이 없습니다."
        )

    return questions
