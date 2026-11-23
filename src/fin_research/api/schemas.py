
from typing import Literal
from pydantic import BaseModel

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