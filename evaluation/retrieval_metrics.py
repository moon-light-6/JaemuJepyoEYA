"""검색 평가셋 로드 및 Gold page 기반 Hit@K 판정."""

import json
import re
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


# ---------------------------------------------------------------------------
# 값 근거 지표
#
# Hit@K는 정답 "쪽"만 비교하므로 정답 값이 없는 청크(예: 표 제목만 있는 청크)가
# 올라와도 성공으로 센다. 아래 지표는 상위 K개 청크 안에 정답 값이 실제로 들어
# 있는지를 함께 본다. 값 판정은 근사 규칙이다.
#   - 숫자 정답: 정답에 포함된 숫자(예: 1,234,567)가 한 청크 안에 모두 있어야 한다.
#   - 한 자리 정답(주석 번호 등): '8. 매출채권'처럼 번호 제목 형식으로 있어야 한다.
#   - 문장 정답: 정답의 단어(2자 이상) 중 절반 이상이 한 청크 안에 있어야 한다.
# ---------------------------------------------------------------------------

NUMBER_PATTERN = re.compile(r"\d{1,3}(?:,\d{3})+")
WORD_PATTERN = re.compile(r"[가-힣A-Za-z0-9]{2,}")
TEXT_COVERAGE_THRESHOLD = 0.5


def expected_value_text(expected_value) -> str:
    """정답 값을 문자열로 바꾼다. 비교 질문처럼 dict이면 JSON 문자열로 만든다."""

    if isinstance(expected_value, str):
        return expected_value
    return json.dumps(expected_value, ensure_ascii=False)


def contains_expected_value(expected_value, text: str) -> bool:
    """청크 텍스트에 정답 값이 들어 있는지 근사 규칙으로 판정한다."""

    normalized = text.replace("　", " ").replace("\n", " ")
    expected = expected_value_text(expected_value).strip()

    numbers = NUMBER_PATTERN.findall(expected)
    if numbers:
        return all(number in normalized for number in numbers)

    if expected.isdigit() and len(expected) <= 2:
        return bool(
            re.search(r"(?:^|\s)" + expected + r"\.\s*[가-힣]", normalized)
        )

    words = WORD_PATTERN.findall(expected)
    if not words:
        return False
    covered = sum(word in normalized for word in words)
    return covered / len(words) >= TEXT_COVERAGE_THRESHOLD


def _result_text(result: dict, chunk_texts: dict[str, str] | None) -> str:
    """검색 결과의 청크 텍스트를 찾는다. 결과에 없으면 chunk_texts에서 찾는다."""

    if result.get("text"):
        return result["text"]
    if chunk_texts is not None:
        return chunk_texts.get(str(result["chunk_id"]), "")
    return ""


def is_value_hit(
    results: list[dict],
    expected_value,
    k: int,
    chunk_texts: dict[str, str] | None = None,
) -> bool:
    """상위 k개 청크 중 하나에 정답 값이 들어 있는지 확인한다(쪽은 보지 않는다)."""

    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")

    return any(
        contains_expected_value(expected_value, _result_text(result, chunk_texts))
        for result in results[:k]
    )


def is_page_value_hit(
    results: list[dict],
    gold_sources: list[dict],
    expected_value,
    k: int,
    chunk_texts: dict[str, str] | None = None,
) -> bool:
    """상위 k개 청크 중 정답 쪽의 청크에 정답 값이 들어 있는지 확인한다(대표 지표)."""

    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")

    gold_keys = {
        (source["source_file"], source["pdf_page"])
        for source in gold_sources
    }

    return any(
        (result["source_file"], result["pdf_page"]) in gold_keys
        and contains_expected_value(
            expected_value, _result_text(result, chunk_texts)
        )
        for result in results[:k]
    )


def summarize_value_hits(items: list[dict]) -> dict:
    """질문별 값 지표 플래그를 요약한다. 정답 값(expected_value)이 없는 질문은 제외한다."""

    valued = [item for item in items if item.get("value_hit_at_3") is not None]
    summary: dict = {"value_total": len(valued)}

    for name in ("value_hit", "page_value_hit"):
        for k in (3, 5):
            key = f"{name}_at_{k}"
            hits = sum(bool(item[key]) for item in valued)
            summary[key] = hits / len(valued) if valued else None
            summary[f"{name}s_at_{k}"] = hits

    return summary
