# [OpenDART 사업보고서 원본 수집을 위한 간단한 클라이언트]

from __future__ import annotations

import json
import os
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import requests


LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
BUSINESS_REPORT_DETAIL_CODE = "A001"


class DartError(Exception):
    """OpenDART 요청 또는 응답 처리 중 발생한 오류."""

class AmbiguousReportError(DartError):
    """접수번호 없이 둘 이상의 유효한 보고서 후보가 발견된 경우."""

@dataclass(frozen=True)
class ReportCandidate:
    # OpenDART 공시목록에서 추린 사업보고서 후보.

    company_name: str
    corp_code: str
    fiscal_year: int
    report_name: str
    rcept_no: str
    rcept_date: str
    filer_name: str
    remarks: str

    @classmethod
    def from_api(cls, item: dict[str, Any], fiscal_year: int) -> "ReportCandidate":
        # OpenDART 목록 한 건을 프로젝트에서 쓰는 후보 객체로 바꿈
        return cls(
            company_name=item.get("corp_name", ""),
            corp_code=item.get("corp_code", ""),
            fiscal_year=fiscal_year,
            report_name=item.get("report_nm", ""),
            rcept_no=item.get("rcept_no", ""),
            rcept_date=item.get("rcept_dt", ""),
            filer_name=item.get("flr_nm", ""),
            remarks=item.get("rm", ""),
        )

    def as_dict(self) -> dict[str, str | int]:
        # 후보 객체 JSON 저장할 용도의 딕셔너리 변환
        return {
            "company_name": self.company_name,
            "corp_code": self.corp_code,
            "fiscal_year": self.fiscal_year,
            "report_name": self.report_name,
            "rcept_no": self.rcept_no,
            "rcept_date": self.rcept_date,
            "filer_name": self.filer_name,
            "remarks": self.remarks,
        }


class DartClient:
    # OpenDART 목록 조회와 원본 ZIP 다운로드를 담당

    def __init__(self, api_key: str, session: requests.Session | None = None, timeout: int = 30):
        if not api_key:
            raise DartError("DART_API_KEY가 비어 있습니다. .env 설정을 확인하세요.")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> "DartClient":
        """
        환경 변수에서 API 키 읽어서 클라이언트 만듦

        python-dotenv가 설치된 환경에서는 프로젝트 루트의 .env도 함께 읽음
        키 값 자체는 로그나 예외 메시지에 넣지 않았음
        """
        try:
            from dotenv import load_dotenv
            load_dotenv()

        except ImportError:
            # 실행 환경에서 이미 환경 변수로 전달했다면? dotenv 없이도 동작
            pass
        return cls(os.getenv("DART_API_KEY", ""))

    # 사업보고서 목록 조회
    def search_business_reports(
        self,
        corp_code: str,
        fiscal_year: int,
        disclosure_start: str,
        disclosure_end: str,
    ) -> list[ReportCandidate]:
        """
        지정한 기업과 사업연도의 사업보고서 후보 검색
        - 사업연도와 공시 접수일을 구분
        - 정정보고서를 포함하여 조회
        - 여러 페이지에 걸친 검색 결과도 모두 조회
        """
        params = {
            "crtfc_key": self.api_key,
            "corp_code": corp_code,
            "bgn_de": disclosure_start.replace("-", ""),
            "end_de": disclosure_end.replace("-", ""),
            "pblntf_ty": "A",
            "pblntf_detail_ty": BUSINESS_REPORT_DETAIL_CODE,
            "last_reprt_at": "N",
            "page_count": 100,
        }

        candidates = []
        page_no = 1

        while True:
            # 현재 페이지 번호를 API 요청에 추가
            params["page_no"] = page_no

            payload = self._get_json(LIST_URL, params)

            # 조회 결과가 없는 경우 검색 종료
            if payload.get("status") == "013":
                break

            items = payload.get("list", [])

            # 사업연도에 맞는 사업보고서만 추출
            target = f"({fiscal_year}."

            for item in items:
                report_name = item.get("report_nm", "")

                if "사업보고서" in report_name and target in report_name:
                    candidates.append(
                        ReportCandidate.from_api(item, fiscal_year)
                    )

            # API가 반환한 전체 페이지 수 확인
            total_page = int(payload.get("total_page", 1))

            if page_no >= total_page:
                break

            page_no += 1

        # 동일한 접수번호의 중복 결과 제거
        return list({
            candidate.rcept_no: candidate for candidate in candidates
        }.values())

    # 사업보고서 확정
    def resolve_report(
        self, candidates: Iterable[ReportCandidate], configured_rcept_no: str | None = None
    ) -> ReportCandidate:
        # 설정된 접수번호로 보고서를 확정하거나 후보가 하나일 때만 선택
        candidate_list = list(candidates)
        if configured_rcept_no:
            for candidate in candidate_list:
                if candidate.rcept_no == configured_rcept_no:
                    return candidate
            raise DartError(
                f"설정한 접수번호 {configured_rcept_no}가 현재 조회 범위의 사업보고서 후보에 없습니다."
            )
        if len(candidate_list) == 1:
            return candidate_list[0]
        if not candidate_list:
            raise DartError("조건에 맞는 사업보고서 후보를 찾지 못했습니다.")
        numbers = ", ".join(candidate.rcept_no for candidate in candidate_list)
        raise AmbiguousReportError(
            f"사업보고서 후보가 {len(candidate_list)}건입니다 ({numbers}). "
            "configs/companies.yaml에 확정한 rcept_no를 입력한 뒤 다시 실행하세요."
        )

    # 원본 ZIP 다운로드 및 검증
    def download_original_zip(self, rcept_no: str, destination: Path) -> int:
        """
        접수번호의 OpenDART 원본 문서를 ZIP으로 내려받고 유효성 검사

        파일이 이미 있고 정상 ZIP이면 네트워크 요청 생략
        다운로드 중간 파일은 .part 확장자로 저장해서 실패한 응답을 원본으로 오인하지 않도록 함
        """
        if destination.exists() and self._is_valid_zip(destination):
            return destination.stat().st_size

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        if temporary.exists():
            temporary.unlink()

        try:
            response = self.session.get(
                DOCUMENT_URL,
                params={"crtfc_key": self.api_key, "rcept_no": rcept_no},
                timeout=self.timeout,
                stream=True,
            )
            response.raise_for_status()
            with temporary.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file.write(chunk)
        except requests.HTTPError as error:
            if temporary.exists():
                temporary.unlink()
            status_code = error.response.status_code if error.response is not None else "알 수 없음"
            raise DartError(f"원본 ZIP 다운로드가 HTTP {status_code} 응답으로 실패했습니다.") from None
        
        except requests.RequestException as error:
            if temporary.exists():
                temporary.unlink()
            raise DartError("원본 ZIP 네트워크 요청에 실패했습니다.") from None

        if not self._is_valid_zip(temporary):
            message = self._read_error_message(temporary)
            temporary.unlink(missing_ok=True)
            raise DartError(f"OpenDART가 유효한 ZIP을 반환하지 않았습니다. {message}".strip())
        temporary.replace(destination)
        return destination.stat().st_size

    # 메타데이터 저장
    def save_metadata(self, candidate: ReportCandidate, zip_path: Path) -> Path:
        # 수집한 ZIP 파일의 메타데이터를 JSON으로 저장
        metadata = candidate.as_dict()
        metadata.update(
            {
                "source": "OpenDART document.xml",
                "downloaded_at": date.today().isoformat(),
                "zip_filename": zip_path.name,
                "zip_size_bytes": zip_path.stat().st_size,
                # 원본 ZIP의 존재만 확인 | 재무제표, 주석의 실제 포함 여부는 별도로 검증해야 할 대상
                "zip_valid": True,
                "financial_statements_and_notes_verified": False,
            }
        )
        metadata_path = zip_path.with_suffix(".json")
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return metadata_path

    # OpenDART API JSON 응답 가져오기
    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        # OpenDART API JSON 응답을 가져오고 상태 코드 검사
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            status_code = (error.response.status_code if error.response is not None else "알 수 없음")
            raise DartError(f"OpenDART API가 HTTP {status_code} 응답을 반환했습니다.") from None

        except requests.RequestException:
            # 요청 URL에 포함된 API 키가 traceback에 출력되지 않도록 처리
            raise DartError("OpenDART API 네트워크 요청에 실패했습니다.") from None

        except ValueError:
            raise DartError("OpenDART API가 JSON이 아닌 응답을 반환했습니다.") from None
        
        if payload.get("status") not in {"000", "013"}:
            raise DartError(f"OpenDART 오류 {payload.get('status')}: {payload.get('message', '알 수 없는 오류')}")
        return payload


    # ZIP 파일 유효성 검사
    @staticmethod
    def _is_valid_zip(path: Path) -> bool:
        # ZIP 구조, 내부 파일 존재 여부 및 압축 데이터의 CRC를 검사
        # XML의 내용이나 재무제표, 주석의 포함 여부는 확인하지 않음
        try:
            with zipfile.ZipFile(path) as archive:
                return bool(archive.namelist()) and archive.testzip() is None
        except (OSError, zipfile.BadZipFile):
            return False

    # ZIP 파일에서 오류 메시지 읽기
    @staticmethod
    def _read_error_message(path: Path) -> str:
        # ZIP 대신 반환된 오류 응답의 앞부분을 텍스트로 읽음
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:300].replace("\n", " ")
        except OSError:
            return ""
