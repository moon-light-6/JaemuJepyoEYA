"""검색 평가셋 로드 및 Gold page 기반 Hit@K 판정."""

import json
from pathlib import Path


def load_eval_dataset(dataset_path: str | Path) -> list[dict]:
    """검증 완료된 평가 질문을 로드한다."""

    dataset_path = Path(dataset_path)

    with dataset_path.open("r", encoding="utf-8") as file:
        dataset = json.load(file)

    if not isinstance(dataset, list) or not dataset:
        raise ValueError("평가셋은 비어 있지 않은 JSON 배열이어야 합니다.")

    verified_questions = []
    seen_ids = set()

    for item in dataset:
        if not isinstance(item, dict):
            raise ValueError("평가 질문은 JSON 객체여야 합니다.")

        if item.get("annotation_status") != "verified":
            continue

        question_id = item.get("id")
        question = item.get("question")
        gold_sources = item.get("gold_sources")

        if not isinstance(question_id, str) or not question_id.strip():
            raise ValueError("평가 질문 ID가 올바르지 않습니다.")

        if question_id in seen_ids:
            raise ValueError(f"중복된 평가 질문 ID: {question_id}")

        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"{question_id}: 질문이 비어 있습니다.")

        if not isinstance(gold_sources, list) or not gold_sources:
            raise ValueError(f"{question_id}: Gold source가 없습니다.")

        for source in gold_sources:
            if not isinstance(source, dict):
                raise ValueError(f"{question_id}: Gold source 형식 오류")

            if (
                not isinstance(source.get("source_file"), str)
                or not source["source_file"].strip()
                or type(source.get("pdf_page")) is not int
                or source["pdf_page"] <= 0
            ):
                raise ValueError(f"{question_id}: 출처 정보 오류")

        seen_ids.add(question_id)
        verified_questions.append(item)

    if not verified_questions:
        raise ValueError("검증 완료된 평가 질문이 없습니다.")

    return verified_questions


def is_hit(
    results: list[dict],
    gold_sources: list[dict],
    k: int,
) -> bool:
    """검색 결과 상위 k개에 Gold source가 있는지 확인한다."""

    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")

    gold_keys = {
        (source["source_file"], source["pdf_page"])
        for source in gold_sources
    }

    for result in results[:k]:
        result_key = (
            result["source_file"],
            result["pdf_page"],
        )

        if result_key in gold_keys:
            return True

    return False


def calculate_hit_rate(
    evaluations: list[dict],
    k: int,
) -> float:
    """질문별 검색 결과를 받아 Hit@K를 계산한다."""

    if not evaluations:
        raise ValueError("평가 결과가 없습니다.")

    hits = sum(
        is_hit(
            item["results"],
            item["gold_sources"],
            k,
        )
        for item in evaluations
    )

    return hits / len(evaluations)
