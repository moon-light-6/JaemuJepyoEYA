"""LLM 응답 → Pydantic 모델 변환: JSON 추출 · 스키마 검증 · 자기 교정 재시도.

1층 (LLM 없이 테스트 가능)
    extract_json(text)          텍스트에서 JSON 부분만 꺼내 dict/list로
    parse_output(text, Schema)  extract_json + Pydantic 검증
2층 (LLM 호출)
    invoke_structured(llm, messages, Schema, max_retries=2)
        호출 → 파싱 실패 시 "틀린 응답 + 에러"를 대화에 붙여 다시 요청
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_PREVIEW_LEN = 200
_MAX_ERROR_ITEMS = 10


class OutputParseError(ValueError):
    """파싱·검증 실패. raw에 원문 응답을 담아 재시도 시 LLM에게 보여준다."""

    def __init__(self, message: str, raw: str):
        super().__init__(message)
        self.raw = raw


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:_PREVIEW_LEN] + ("…" if len(flat) > _PREVIEW_LEN else "")


# ─────────────────────────── 1층 ───────────────────────────


def extract_json(text: str) -> dict | list:
    """응답 텍스트에서 JSON을 꺼낸다. (코드 블록 → 앞뒤 잡담 제거 순)

    문법 오류(끝 쉼표 등)는 고치지 않는다. 그건 재시도로 LLM이 고치게 한다.
    """
    if not text or not text.strip():
        raise OutputParseError("응답이 비어 있습니다.", text or "")

    # ① ```json ... ``` 코드 블록이 있으면 그 안만
    m = _FENCE_RE.search(text)
    candidate = (m.group(1) if m else text).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # ② 앞뒤 잡담 제거: 처음 여는 괄호 ~ 같은 종류의 마지막 닫는 괄호
    starts = [i for i in (candidate.find("{"), candidate.find("[")) if i != -1]
    if not starts:
        raise OutputParseError(f"응답에서 JSON을 찾지 못했습니다: {_preview(text)}", text)
    start = min(starts)
    end = candidate.rfind("}" if candidate[start] == "{" else "]")
    if end <= start:
        raise OutputParseError(f"JSON이 닫히지 않았습니다: {_preview(text)}", text)

    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as e:
        raise OutputParseError(
            f"JSON 문법 오류 ({e.msg}, {e.lineno}행 {e.colno}열): {_preview(text)}", text
        ) from e


def parse_output(text: str, schema: type[T]) -> T:
    """텍스트 → schema 인스턴스. 실패 시 어느 필드가 왜 틀렸는지 담아 OutputParseError."""
    data = extract_json(text)
    try:
        return schema.model_validate(data)
    except ValidationError as e:
        items = [
            f"{'.'.join(str(p) for p in err['loc']) or '(최상위)'}: {err['msg']}"
            for err in e.errors()[:_MAX_ERROR_ITEMS]
        ]
        more = e.error_count() - len(items)
        if more > 0:
            items.append(f"외 {more}건")
        raise OutputParseError(f"{schema.__name__} 스키마 검증 실패 — " + "; ".join(items), text) from e


# ─────────────────────────── 2층 ───────────────────────────


def format_instructions(schema: type[BaseModel]) -> str:
    """프롬프트에 붙일 출력 형식 지시문."""
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)
    return (
        "아래 JSON 스키마를 따르는 JSON 객체 하나만 출력하세요.\n"
        "인사말·설명·코드 블록 없이 JSON만 출력합니다.\n\n"
        f"{schema_json}"
    )


def _retry_message(error: OutputParseError) -> str:
    return (
        f"이전 응답을 처리하지 못했습니다.\n오류: {error}\n"
        "같은 내용을 스키마에 맞는 JSON 객체 하나로만 다시 출력하세요."
    )


def _prepare_messages(
    messages: str | Sequence[BaseMessage], schema: type[BaseModel]
) -> list[BaseMessage]:
    """원본을 건드리지 않도록 복사한 뒤 형식 지시를 붙인다."""
    msgs: list[BaseMessage] = (
        [HumanMessage(messages)] if isinstance(messages, str) else list(messages)
    )
    msgs.append(HumanMessage(format_instructions(schema)))
    return msgs


def invoke_structured(
    llm: BaseChatModel,
    messages: str | Sequence[BaseMessage],
    schema: type[T],
    max_retries: int = 2,
) -> T:
    """LLM을 호출해 schema 인스턴스를 받는다. 실패하면 최대 max_retries번 자기 교정 재시도.

    messages: 문자열(사용자 메시지 하나) 또는 메시지 리스트. 원본 리스트는 수정하지 않는다.
    """
    msgs = _prepare_messages(messages, schema)

    attempts = max_retries + 1
    last_error: OutputParseError | None = None
    for _ in range(attempts):
        raw = str(llm.invoke(msgs).text)
        try:
            return parse_output(raw, schema)
        except OutputParseError as e:
            last_error = e
            msgs += [AIMessage(raw), HumanMessage(_retry_message(e))]

    assert last_error is not None
    raise OutputParseError(f"{attempts}회 시도 후 실패 — {last_error}", last_error.raw) from last_error


async def ainvoke_structured(
    llm: BaseChatModel,
    messages: str | Sequence[BaseMessage],
    schema: type[T],
    max_retries: int = 2,
) -> T:
    """invoke_structured의 async 버전. async 노드에서 이벤트 루프를 막지 않는다."""
    msgs = _prepare_messages(messages, schema)

    attempts = max_retries + 1
    last_error: OutputParseError | None = None
    for _ in range(attempts):
        raw = str((await llm.ainvoke(msgs)).text)
        try:
            return parse_output(raw, schema)
        except OutputParseError as e:
            last_error = e
            msgs += [AIMessage(raw), HumanMessage(_retry_message(e))]

    assert last_error is not None
    raise OutputParseError(f"{attempts}회 시도 후 실패 — {last_error}", last_error.raw) from last_error


if __name__ == "__main__":
    from typing import Literal

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    # 테스트용 임시 스키마 (실제 위치는 evidence/models.py 또는 데모 노드 쪽)
    class Claim(BaseModel):
        company: str
        year: int
        metric: str
        value: float
        unit: str
        basis: Literal["연결", "별도", "불명"] = "불명"
        quote: str

    class ClaimList(BaseModel):
        claims: list[Claim]

    print("── 1층: parse_output ──")
    cases = [
        '{"claims": []}',
        '네, 결과입니다!\n```json\n{"claims": []}\n```',
        '결과: {"claims": []} 이상입니다.',
        '{"claims": [{"company": "삼성전자", "year": "2024", "metric": "매출액", '
        '"value": 300.9, "unit": "조원", "quote": "..."}]}',
        '{"claims": [{"company": "삼성전자"}]}',
        '죄송하지만 추출할 수 없습니다.',
    ]
    for c in cases:
        try:
            print("OK  ", parse_output(c, ClaimList))
        except OutputParseError as e:
            print("FAIL", e)

    print("\n── 2층: invoke_structured (가짜 LLM, API 호출 없음) ──")
    fake = FakeListChatModel(responses=["추출 결과는 다음과 같습니다.", '{"claims": []}'])
    print("재시도 후 성공:", invoke_structured(fake, "보고서에서 수치를 추출해줘", ClaimList))

    fake = FakeListChatModel(responses=["몰라요"] * 3)
    try:
        invoke_structured(fake, "보고서에서 수치를 추출해줘", ClaimList)
    except OutputParseError as e:
        print("최종 실패:", e)
