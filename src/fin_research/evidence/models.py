"""보고서에서 인용하는 검색 근거의 데이터 모델.

검색 결과의 원문은 그래프 state에 넣지 않고, 보고서 실행 단위의
``EvidenceRegistry``가 보관한다. state와 답변에는 짧은 ``S#`` ID만 남긴다.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_EVIDENCE_ID_PATTERN = re.compile(r"S[1-9]\d*")


class EvidenceCandidate(BaseModel):
    """검색기가 반환한, 아직 출처 번호가 없는 근거 한 건.

    XML 검색 결과는 ``source_xpath``를, PDF 검색 결과는 ``pdf_page``를
    위치 정보로 가진다. 같은 ``source_file``의 같은 ``chunk_id``가 하나의
    중복 제거 단위다.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    source_file: str = Field(description="원본 공시 파일명")
    chunk_id: str = Field(description="검색 인덱스의 고유 청크 ID")
    text: str = Field(description="인용 근거가 되는 검색 원문")
    source_xpath: str | None = Field(default=None, description="XML 원본 요소 XPath")
    pdf_page: int | None = Field(default=None, ge=1, description="PDF 원본 페이지 번호")
    source_format: str | None = Field(default=None, description="xml 또는 pdf 등 원본 형식")
    title: str | None = Field(default=None, description="원본 표 또는 섹션 제목")

    @field_validator("source_file", "chunk_id", "text")
    @classmethod
    def require_nonempty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("빈 값은 출처 근거로 등록할 수 없습니다.")
        return value

    @field_validator("source_xpath")
    @classmethod
    def normalize_xpath(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def require_source_location(self) -> EvidenceCandidate:
        if self.source_xpath is None and self.pdf_page is None:
            raise ValueError("XML XPath 또는 PDF 페이지 중 하나는 필요합니다.")
        return self

    @property
    def source_key(self) -> tuple[str, str]:
        """한 보고서 안에서 동일 근거인지 판정하는 키."""

        return self.source_file, self.chunk_id


class Evidence(EvidenceCandidate):
    """레지스트리에 등록되어 전역 ``S#`` 번호가 붙은 근거 한 건."""

    evidence_id: str = Field(description="보고서 전체에서 유일한 출처 ID. 예: S1")

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        if not _EVIDENCE_ID_PATTERN.fullmatch(value):
            raise ValueError("evidence_id는 S1, S2처럼 양의 번호를 써야 합니다.")
        return value
