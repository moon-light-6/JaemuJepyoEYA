"""공식 공시 PDF 원본의 수집 이력과 페이지별 텍스트 점검을 처리한다."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pymupdf as fitz
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[3]
OFFICIAL_DART_HOSTS = {"dart.fss.or.kr"}


class PdfDocumentError(Exception):
    """PDF 원본 다운로드 또는 구조 점검을 계속할 수 없을 때 발생한다."""


def sha256_file(file_path: Path) -> str:
    """큰 PDF도 메모리에 모두 올리지 않고 SHA-256 해시를 계산한다."""
    digest = hashlib.sha256()
    with file_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dart_pdf_url(rcept_no: str, dcm_no: str) -> str:
    """DART PDF 원본 다운로드 URL을 접수번호와 문서번호로 만든다."""
    return f"https://dart.fss.or.kr/pdf/download/pdf.do?rcp_no={rcept_no}&dcm_no={dcm_no}"


def dart_pdf_download_page_url(rcept_no: str, dcm_no: str) -> str:
    """DART가 PDF 다운로드 세션을 준비하는 공식 안내 페이지 URL을 만든다."""
    return f"https://dart.fss.or.kr/pdf/download/main.do?rcp_no={rcept_no}&dcm_no={dcm_no}"


def ensure_official_dart_url(source_url: str) -> None:
    """DART 공식 도메인이 아닌 URL은 공식 원본으로 저장하지 않도록 막는다."""
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_DART_HOSTS:
        raise PdfDocumentError(
            "공식 DART PDF는 https://dart.fss.or.kr URL에서만 수집할 수 있습니다."
        )


def download_official_pdf(
    source_url: str,
    output_path: Path,
    timeout_seconds: int = 60,
    referer_url: str | None = None,
) -> dict[str, Any]:
    """공식 DART URL에서 PDF를 내려받고 응답·파일 유효성을 확인해 저장한다.

    HTML 오류 페이지나 임의 생성 파일을 PDF 원본으로 저장하지 않기 위해 PDF 서명과
    최종 응답 도메인을 함께 검사한다. 성공한 파일만 최종 경로로 이동한다.
    """
    ensure_official_dart_url(source_url)
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        if referer_url:
            ensure_official_dart_url(referer_url)
            # DART는 다운로드 화면을 먼저 방문한 세션의 PDF 응답을 다르게 처리할 수 있다.
            session.get(referer_url, timeout=timeout_seconds).raise_for_status()
        response = session.get(
            source_url,
            timeout=timeout_seconds,
            allow_redirects=True,
            headers={"Referer": referer_url} if referer_url else None,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise PdfDocumentError(f"공식 PDF 다운로드에 실패했습니다: {error}") from error

    final_url = response.url
    ensure_official_dart_url(final_url)
    content = response.content
    if not content.startswith(b"%PDF-"):
        raise PdfDocumentError(
            "공식 URL 응답이 PDF 서명으로 시작하지 않습니다. 오류 HTML을 원본으로 저장하지 않았습니다."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".part")
    try:
        temporary_path.write_bytes(content)
        # PyMuPDF로도 열리는지 확인한 뒤에만 원본 파일명으로 확정한다.
        with fitz.open(temporary_path) as document:
            if not document.is_pdf or document.page_count == 0:
                raise PdfDocumentError("다운로드한 파일이 비어 있거나 정상 PDF가 아닙니다.")
        temporary_path.replace(output_path)
    except PdfDocumentError:
        # PDF 자체가 유효하지 않으면 임시 파일 삭제 후 오류 전달
        temporary_path.unlink(missing_ok=True)
        raise

    except (OSError, RuntimeError, fitz.FileDataError) as error:
        # 파일 저장 또는 PDF 해석 과정의 오류 처리
        temporary_path.unlink(missing_ok=True)

        raise PdfDocumentError(f"PDF 파일 검증 또는 저장에 실패했습니다: {error}") from error

    return {
        "format": "pdf",
        "official_source_verified": True,
        "source_url": source_url,
        "final_url": final_url,
        "source_host": urlparse(final_url).hostname,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "filename": output_path.name,
        "size_bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
        "http_content_type": response.headers.get("Content-Type"),
    }


def extract_pdf_pages(pdf_path: Path) -> dict[str, Any]:
    """PyMuPDF로 각 페이지의 원문 텍스트와 품질 점검 정보를 추출한다.

    이 함수는 OCR이나 문자 보정을 하지 않는다. 빈 페이지와 U+FFFD 대체문자만
    기계적으로 기록하며, 그 밖의 문자 품질은 후속 검토 대상임을 명시한다.
    """
    if not pdf_path.is_file():
        raise PdfDocumentError(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")

    pages: list[dict[str, Any]] = []
    try:
        with fitz.open(pdf_path) as document:
            if not document.is_pdf:
                raise PdfDocumentError("입력 파일이 PDF가 아닙니다.")
            for page_number, page in enumerate(document, start=1):
                text = page.get_text("text")
                replacement_count = text.count("\ufffd")
                control_count = sum(
                    1 for character in text if ord(character) < 32 and character not in "\n\r\t"
                )
                pages.append(
                    {
                        "page_number": page_number,
                        "source": {"format": "pdf", "page_number": page_number},
                        "text": text,
                        "character_count": len(text),
                        "is_blank": not text.strip(),
                        "replacement_character_count": replacement_count,
                        "unexpected_control_character_count": control_count,
                    }
                )
    except (OSError, RuntimeError, fitz.FileDataError) as error:
        if isinstance(error, PdfDocumentError):
            raise
        raise PdfDocumentError(f"PDF를 PyMuPDF로 열거나 텍스트를 추출하지 못했습니다: {error}") from error

    blank_pages = [page["page_number"] for page in pages if page["is_blank"]]
    replacement_pages = [
        page["page_number"] for page in pages if page["replacement_character_count"] > 0
    ]
    control_character_pages = [
        page["page_number"] for page in pages if page["unexpected_control_character_count"] > 0
    ]
    return {
        "pages": pages,
        "summary": {
            "page_count": len(pages),
            "nonblank_page_count": len(pages) - len(blank_pages),
            "text_extractable": any(page["text"].strip() for page in pages),
            "blank_page_numbers": blank_pages,
            "replacement_character_page_numbers": replacement_pages,
            "unexpected_control_character_page_numbers": control_character_pages,
            "character_encoding_review_required": bool(replacement_pages or control_character_pages),
        },
    }


def inspect_pdf(pdf_path: Path, source_metadata: dict[str, Any]) -> dict[str, Any]:
    """PDF 원본 해시·출처와 페이지별 텍스트 점검 결과를 하나의 JSON으로 만든다."""
    extracted = extract_pdf_pages(pdf_path)
    return {
        "source": {
            **source_metadata,
            "pdf_path": str(pdf_path),
            "sha256": sha256_file(pdf_path),
        },
        "format_validation": {
            "format": "pdf",
            "source_locator": "page_number",
            "xml_comparison_performed": False,
            "note": "PDF와 XML의 내용 동일성은 이 점검에서 가정하거나 판정하지 않습니다.",
        },
        **extracted,
    }


def save_pdf_inspection(result: dict[str, Any], output_path: Path) -> Path:
    """PDF 페이지별 텍스트와 점검 결과를 UTF-8 JSON으로 저장한다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path
