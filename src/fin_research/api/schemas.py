
from typing import Literal
from pydantic import BaseModel, Field

# API 응답 스키마 정의
class JobUploadResponse(BaseModel):
    id: str
    status: Literal["uploaded"]
    filename: str
    file_size: int
    page_count: int
    message: str | None = None

# 업로드된 PDF 작업 상태 응답 스키마
class JobStatusResponse(BaseModel):
    id: str
    status: Literal["uploaded"]
    file_size: int
    page_count: int

# 업로드된 PDF 전체 본문 추출 결과 응답 스키마
class JobExtractionResponse(BaseModel):
    job_id: str
    total_pages: int
    nonblank_pages: int
    blank_pages: int
    character_count: int
    status: Literal["extracted"]

# 업로드된 PDF 작업 상태 응답 스키마 (업로드 및 추출 상태 포함)
class JobStatusResponse(BaseModel):
    id: str
    status: Literal["uploaded", "extracted"]
    file_size: int
    page_count: int

# 전체 실행(조사·보고서·검증) 요청 스키마
class ResearchRequest(BaseModel):
    request: str = Field(min_length=1, max_length=2000)
    # 동명 기업 후보 중 하나를 고를 때만 지정 (DART 고유번호 8자리)
    corp_code: str | None = Field(default=None, pattern=r"^\d{8}$")

# 전체 실행 접수 응답 스키마
class ResearchAcceptedResponse(BaseModel):
    id: str
    status: Literal["queued"]

# 전체 실행 상태·결과 응답 스키마
class ResearchJobResponse(BaseModel):
    id: str
    status: Literal[
        "queued", "running", "success", "partial_success", "failed", "needs_clarification"
    ]
    stage: str | None = None
    result: dict | None = None
    created_at: str
    finished_at: str | None = None
