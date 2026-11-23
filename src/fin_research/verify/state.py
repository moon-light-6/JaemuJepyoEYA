"""검증 그래프의 state와 데이터 구조 (Claim, Verdict, VerifyState).

흐름: PDF → claim_extract → verify_claim ×N (병렬) → aggregate

- 내용물(LLM 출력·노드 간 데이터)은 Pydantic → invoke_structured로 바로 검증
- 그래프 state는 TypedDict → 노드는 바꾼 칸만 dict로 반환
- LLM은 주장을 "뽑기"만 한다. 단위 환산·값 비교·판정은 코드(numbers.py)가 한다.
  그래서 Claim.value는 원문 단위 그대로, 원 단위 값은 Verdict에만 둔다.
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

# ----------------------
# 선택지
# ----------------------

Scope = Literal["consolidated", "separate", "unknown"]
Period = Literal["current", "prior", "unknown"]
VerdictLabel = Literal["match", "mismatch", "not_found", "unverifiable"]
"""
- match:        기준값과 허용 오차 안에서 같음
- mismatch:     기준값과 다름
- not_found:    있어야 할 항목인데 DB·문서에서 못 찾음 (데이터 문제)
- unverifiable: 검증할 수 없는 주장 (숫자 아님, 업종에 없는 항목, 연도 불명확 등)
"""

VERDICT_LABELS: tuple[str, ...] = ("match", "mismatch", "not_found", "unverifiable")


# ----------------------
# 주장 추출
# ----------------------

class ClaimDraft(BaseModel):
    """LLM이 PDF에서 뽑는 숫자 주장 하나. id는 코드가 붙인다."""

    text: str = Field(description="PDF 원문 문장 그대로 (요약·수정 금지)")
    item: str = Field(description="재무 항목명. 예: 매출액, 영업이익, 부채비율")
    value: float | None = Field(
        default=None,
        description="원문에 적힌 숫자 그대로. 단위 환산하지 않는다. 예: '32.7조원' → 32.7",
    )
    unit: str | None = Field(default=None, description="원문 단위 그대로. 예: 조원, 억원, 백만원, %")
    year: int | None = Field(default=None, description="주장이 가리키는 사업연도. 불명확하면 null")
    scope: Scope = Field(default="unknown", description="연결/별도. 원문에 없으면 unknown")
    period: Period = Field(
        default="unknown",
        description="보고서 기준 당기(current)인지 전기(prior)인지. 판단이 어려우면 unknown",
    )
    page: int | None = Field(default=None, description="주장이 있는 PDF 쪽 번호 (1부터)")


class Claim(ClaimDraft):
    """그래프 안에서 쓰는 주장. 순서대로 C1, C2 … id를 붙인다."""

    id: str


class ClaimExtraction(BaseModel):
    """claim_extract 노드의 LLM 출력 형태."""

    claims: list[ClaimDraft] = Field(default_factory=list)


def number_claims(drafts: list[ClaimDraft]) -> list[Claim]:
    """LLM 출력에 C1, C2 … id를 붙인다 (id는 LLM에게 맡기지 않는다)."""
    return [Claim(id=f"C{i}", **draft.model_dump()) for i, draft in enumerate(drafts, start=1)]


# ----------------------
# 검증 결과
# ----------------------

class VerifyEvidence(BaseModel):
    """판정 근거의 위치. 원문은 넣지 않고 찾아갈 수 있는 정보만 둔다.

    research의 EvidenceRef와 달리 XML 위치(source_xpath)를 가진다.
    """

    id: str = Field(description="근거 ID. 예: C1.db, C1.S1")
    kind: Literal["db", "xml", "pdf"] = Field(description="근거 종류")
    source_file: str | None = None
    source_xpath: str | None = None
    pdf_page: int | None = None
    chunk_id: str | None = None


class Verdict(BaseModel):
    """주장 하나의 검증 결과. 값 비교와 label은 코드가 정한다."""

    claim_id: str
    label: VerdictLabel
    claim_value_won: float | None = Field(default=None, description="주장 값을 원 단위(비율은 %)로 환산한 값")
    db_value_won: float | None = Field(default=None, description="DB 기준값 (원 단위, 비율은 %)")
    diff_ratio: float | None = Field(default=None, description="상대 오차 (claim - db) / |db|")
    account_id: str | None = Field(default=None, description="DB 계정 코드. 예: dart_OperatingIncomeLoss")
    account_nm: str | None = Field(default=None, description="DB 계정명. 예: 영업이익")
    rcept_no: str | None = Field(default=None, description="기준값을 가져온 공시 접수번호")
    evidence: list[VerifyEvidence] = Field(default_factory=list)
    explanation: str = Field(default="", description="사람이 읽을 한 줄 설명")


def summarize(verdicts: list[Verdict]) -> dict[str, int]:
    """판정별 개수. 없는 판정도 0으로 넣어 화면이 항상 같은 키를 받게 한다."""
    counts = {label: 0 for label in VERDICT_LABELS}
    for verdict in verdicts:
        counts[verdict.label] += 1
    counts["total"] = len(verdicts)
    return counts


# ----------------------
# 그래프 state (TypedDict)
# ----------------------

class VerifyInput(TypedDict):
    """그래프 입력. company를 주지 않으면 PDF에서 추정한다."""

    pdf_path: str
    company: str | None


class VerifyState(TypedDict, total=False):
    pdf_path: str
    company: str | None
    corp_code: str | None
    fiscal_year: int | None
    rcept_no: str | None
    claims: list[Claim]
    # verify_claim을 주장마다 Send로 병렬 실행 → 각자 [Verdict 하나]를 반환 → 이어 붙임
    verdicts: Annotated[list[Verdict], operator.add]
    summary: dict[str, int]
    errors: Annotated[list[str], operator.add]
    step: str


class ClaimVerifyInput(TypedDict):
    """Send로 verify_claim 노드 하나에 넘기는 입력."""

    claim: Claim
    corp_code: str | None
    fiscal_year: int | None
    rcept_no: str | None


class VerifyOutput(TypedDict):
    """그래프 출력이자 React에 넘길 결과의 뼈대."""

    company: str | None
    corp_code: str | None
    claims: list[Claim]
    verdicts: list[Verdict]
    summary: dict[str, int]
    errors: list[str]
