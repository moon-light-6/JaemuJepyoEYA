
#재무제표 검증 시스템 FastAPI 서버.

import json
from pathlib import Path
from uuid import UUID, uuid4

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile

from fin_research.api.schemas import (
    JobUploadResponse,
    JobStatusResponse,
    JobExtractionResponse,
)
from fin_research.ingestion.pdf_parser import (
    PdfDocumentError,
    extract_pdf_pages,
)


# [1. 서버 및 기본 설정]
app = FastAPI(
    title="재무제표 검증 API",
    description="재무제표 Deep Research 검증 시스템",
    version="0.1.0",
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]

UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"
EXTRACT_DIR = PROJECT_ROOT / "data" / "processed" / "uploads"

MAX_FILE_SIZE = 30_000_000  # 30MB
PREVIEW_TEXT_LENGTH = 1500



# [2. 공통 함수]
def get_pdf_path(job_id: UUID) -> Path:
    """작업 ID로 업로드된 PDF 경로를 찾는다."""

    path = UPLOAD_DIR / f"{job_id}.pdf"

    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="업로드한 PDF를 찾을 수 없습니다.",
        )

    return path


def get_extract_path(job_id: UUID) -> Path:
    """작업 ID에 해당하는 텍스트 추출 JSON 경로를 반환한다."""

    return EXTRACT_DIR / f"{job_id}_pages.json"


def validate_pdf(content: bytes) -> int:
    """PDF 파일 형식과 암호화 여부를 확인하고 페이지 수를 반환한다."""

    if not content:
        raise HTTPException(400, "빈 파일입니다.")

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, "파일 크기는 최대 30MB입니다.")

    if not content.startswith(b"%PDF-"):
        raise HTTPException(400, "유효한 PDF 파일이 아닙니다.")

    try:
        with pymupdf.open(stream=content, filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise HTTPException(400, "암호화된 PDF는 지원하지 않습니다.")

            if len(pdf) == 0:
                raise HTTPException(400, "PDF에 페이지가 없습니다.")

            return len(pdf)

    except (pymupdf.FileDataError, pymupdf.EmptyFileError):
        raise HTTPException(400, "PDF를 읽을 수 없습니다.")



# [3. API 엔드포인트]
# 3-1. 서버 상태 확인
@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "message": "API 서버 정상 작동",
    }


# 3-2. PDF 업로드
@app.post(
    "/api/jobs",
    response_model=JobUploadResponse,
    status_code=201,
)
async def create_job(
    file: UploadFile = File(...),
    message: str | None = Form(None),
):
    """PDF를 검증하고 로컬 저장소에 업로드한다."""

    filename = Path(file.filename or "").name

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드할 수 있습니다.")

    # 파일 읽기 및 검증
    try:
        content = await file.read(MAX_FILE_SIZE + 1)
    finally:
        await file.close()

    page_count = validate_pdf(content)

    # 작업 ID 생성 및 PDF 저장
    job_id = uuid4()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    save_path = UPLOAD_DIR / f"{job_id}.pdf"
    save_path.write_bytes(content)

    return JobUploadResponse(
        id=str(job_id),
        status="uploaded",
        filename=filename,
        file_size=len(content),
        page_count=page_count,
        message=message,
    )


# 3-3. 작업 상태 조회
@app.get(
    "/api/jobs/{job_id}",
    response_model=JobStatusResponse,
)
def get_job_status(job_id: UUID):
    """PDF 업로드 및 텍스트 추출 여부를 조회한다."""

    pdf_path = get_pdf_path(job_id)
    extract_path = get_extract_path(job_id)

    status = "extracted" if extract_path.is_file() else "uploaded"

    with pymupdf.open(pdf_path) as pdf:
        page_count = len(pdf)

    return JobStatusResponse(
        id=str(job_id),
        status=status,
        file_size=pdf_path.stat().st_size,
        page_count=page_count,
    )


# 3-4. PDF 텍스트 미리보기
@app.get("/api/jobs/{job_id}/preview")
def preview_pdf(
    job_id: UUID,
    pages: int = Query(default=3, ge=1, le=10),
):
    """PDF의 앞쪽 페이지 텍스트를 미리보기로 반환한다."""

    pdf_path = get_pdf_path(job_id)

    with pymupdf.open(pdf_path) as pdf:
        results = []

        for index in range(min(pages, len(pdf))):
            page = pdf.load_page(index)

            results.append({
                "page": index + 1,
                "text": page.get_text("text")[:PREVIEW_TEXT_LENGTH],
            })

        return {
            "job_id": str(job_id),
            "total_pages": len(pdf),
            "extracted_pages": len(results),
            "pages": results,
        }


# 3-5. PDF 전체 텍스트 추출
@app.post(
    "/api/jobs/{job_id}/extract",
    response_model=JobExtractionResponse,
)
def extract_report(job_id: UUID):
    """PDF 전체 페이지의 텍스트를 추출해 JSON으로 저장한다."""

    pdf_path = get_pdf_path(job_id)

    # 기존 PDF 파서 재사용
    try:
        result = extract_pdf_pages(pdf_path)
    except PdfDocumentError:
        raise HTTPException(
            status_code=422,
            detail="PDF 텍스트 추출에 실패했습니다.",
        )

    summary = result["summary"]

    if not summary["text_extractable"]:
        raise HTTPException(
            status_code=422,
            detail="추출 가능한 텍스트가 없습니다. OCR 확인이 필요합니다.",
        )

    # JSON 저장
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = get_extract_path(job_id)

    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return JobExtractionResponse(
        job_id=str(job_id),
        total_pages=summary["page_count"],
        nonblank_pages=summary["nonblank_page_count"],
        blank_pages=len(summary["blank_page_numbers"]),
        character_count=sum(
            page["character_count"]
            for page in result["pages"]
        ),
        status="extracted",
    )
