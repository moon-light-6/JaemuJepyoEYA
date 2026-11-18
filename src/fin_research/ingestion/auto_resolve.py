"""기업명·종목코드만으로 OpenDART/DART 식별값을 자동으로 찾는다.

- corp_code: OpenDART 고유번호 파일(corpCode.xml, ZIP)에서 기업명·종목코드로 조회
- rcept_no: 기존 DartClient.search_business_reports() 후보 중 규칙으로 선택
- dcm_no: OpenDART API에는 없으므로 DART 공시 뷰어 페이지(HTML)에서 추출
- 재무제표 계정 금액: OpenDART 단일회사 전체 재무제표 API(fnlttSinglAcntAll)
"""

from __future__ import annotations

import io
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

from fin_research.ingestion.dart_client import DartClient, DartError, ReportCandidate


CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
FINANCIAL_ACCOUNTS_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do"
BUSINESS_REPORT_CODE = "11011"  # 사업보고서


class CorpLookupError(DartError):
    """기업명·종목코드로 고유번호를 확정하지 못한 경우."""


@dataclass(frozen=True)
class CorpInfo:
    corp_code: str
    corp_name: str
    stock_code: str

    @property
    def is_listed(self) -> bool:
        return bool(self.stock_code.strip())


# ---------------------------------------------------------------------------
# 1. corp_code
# ---------------------------------------------------------------------------

def parse_corp_code_xml(xml_bytes: bytes) -> list[CorpInfo]:
    """CORPCODE.xml의 <list> 항목을 CorpInfo 목록으로 바꾼다."""

    root = ET.fromstring(xml_bytes)
    corps = []
    for item in root.iter("list"):
        corp_code = (item.findtext("corp_code") or "").strip()
        if not corp_code:
            continue
        corps.append(
            CorpInfo(
                corp_code=corp_code,
                corp_name=(item.findtext("corp_name") or "").strip(),
                stock_code=(item.findtext("stock_code") or "").strip(),
            )
        )
    if not corps:
        raise CorpLookupError("고유번호 파일에서 기업 목록을 읽지 못했습니다.")
    return corps


def load_corp_codes(
    client: DartClient,
    cache_path: Path,
    max_age_days: int = 30,
    refresh: bool = False,
) -> list[CorpInfo]:
    """고유번호 파일을 내려받아 캐시하고 목록을 돌려준다. 캐시가 새로우면 재사용한다."""

    fresh = (
        cache_path.is_file()
        and time.time() - cache_path.stat().st_mtime < max_age_days * 86_400
    )
    if fresh and not refresh:
        return parse_corp_code_xml(cache_path.read_bytes())

    try:
        response = client.session.get(
            CORP_CODE_URL, params={"crtfc_key": client.api_key}, timeout=client.timeout
        )
        response.raise_for_status()
    except requests.RequestException:
        # 요청 URL의 API 키가 출력되지 않도록 원인 예외를 숨긴다.
        raise CorpLookupError("OpenDART 고유번호 파일 요청에 실패했습니다.") from None

    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            xml_name = next(name for name in archive.namelist() if name.lower().endswith(".xml"))
            xml_bytes = archive.read(xml_name)
    except (zipfile.BadZipFile, StopIteration):
        message = response.content[:300].decode("utf-8", errors="replace")
        raise CorpLookupError(f"OpenDART가 고유번호 ZIP을 반환하지 않았습니다. {message}") from None

    corps = parse_corp_code_xml(xml_bytes)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(xml_bytes)
    return corps


def normalize_corp_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "")
    text = re.sub(r"\(주\)|㈜|주식회사", "", text)
    return re.sub(r"[\s.\-·]", "", text).lower()


def find_corp(query: str, corps: Iterable[CorpInfo]) -> CorpInfo:
    """8자리 고유번호, 6자리 종목코드, 기업명 순서로 기업을 찾는다.

    같은 이름이 여러 개면 상장사를 우선하고, 그래도 여럿이면 후보를 보여 주며 멈춘다.
    """

    query = (query or "").strip()
    if not query:
        raise CorpLookupError("기업명 또는 코드가 비어 있습니다.")
    corps = list(corps)

    if re.fullmatch(r"\d{8}", query):
        matches = [corp for corp in corps if corp.corp_code == query]
    elif re.fullmatch(r"[0-9A-Z]{6}", query.upper()) and any(c.isdigit() for c in query):
        matches = [corp for corp in corps if corp.stock_code.upper() == query.upper()]
    else:
        matches = [corp for corp in corps if corp.corp_name == query]
        if not matches:
            key = normalize_corp_name(query)
            matches = [corp for corp in corps if normalize_corp_name(corp.corp_name) == key]

    if len(matches) > 1:
        listed = [corp for corp in matches if corp.is_listed]
        if len(listed) == 1:
            return listed[0]
        matches = listed or matches

    if len(matches) == 1:
        return matches[0]

    if not matches:
        key = normalize_corp_name(query)
        similar = [
            corp for corp in corps
            if corp.is_listed and key and key in normalize_corp_name(corp.corp_name)
        ][:8]
        hint = ", ".join(f"{c.corp_name}({c.stock_code})" for c in similar)
        raise CorpLookupError(
            f"'{query}'에 해당하는 기업을 찾지 못했습니다."
            + (f" 비슷한 상장사: {hint}" if hint else "")
        )

    listing = ", ".join(f"{c.corp_name}/{c.corp_code}/{c.stock_code or '-'}" for c in matches[:8])
    raise CorpLookupError(
        f"'{query}' 후보가 {len(matches)}개입니다: {listing}. 종목코드나 고유번호로 지정하세요."
    )


# ---------------------------------------------------------------------------
# 2. rcept_no
# ---------------------------------------------------------------------------

def choose_report(
    candidates: Iterable[ReportCandidate],
    configured_rcept_no: str | None = None,
    prefer: str = "latest",
) -> tuple[ReportCandidate, str]:
    """사업보고서 후보 중 하나를 고르고 선택 사유를 함께 돌려준다.

    정정공시가 있으면 후보가 여러 건이다. 기본값(latest)은 가장 최근 접수본을 쓴다.
    configs/companies.yaml에 rcept_no가 있으면 그 값을 우선한다.
    """

    candidate_list = sorted(candidates, key=lambda candidate: candidate.rcept_no)
    if configured_rcept_no:
        for candidate in candidate_list:
            if candidate.rcept_no == configured_rcept_no:
                return candidate, "companies.yaml의 rcept_no 사용"
        raise DartError(
            f"설정한 접수번호 {configured_rcept_no}가 조회된 사업보고서 후보에 없습니다."
        )
    if not candidate_list:
        raise DartError("조건에 맞는 사업보고서 후보를 찾지 못했습니다.")
    if len(candidate_list) == 1:
        return candidate_list[0], "후보 1건"

    if prefer not in {"latest", "original"}:
        raise ValueError("prefer는 latest 또는 original이어야 합니다.")
    chosen = candidate_list[-1] if prefer == "latest" else candidate_list[0]
    others = ", ".join(
        f"{c.rcept_no}({c.report_name})" for c in candidate_list if c is not chosen
    )
    label = "최신 접수본" if prefer == "latest" else "최초 접수본"
    return chosen, f"후보 {len(candidate_list)}건 중 {label} 선택 (제외: {others})"


# ---------------------------------------------------------------------------
# 3. dcm_no
# ---------------------------------------------------------------------------

def extract_dcm_no(html: str, rcept_no: str) -> str | None:
    """DART 공시 뷰어 HTML에서 본문 PDF의 문서번호(dcm_no)를 찾는다."""

    rcp = re.escape(rcept_no)
    patterns = [
        # 상단 '다운로드' 버튼: openPdfDownload('접수번호', '문서번호')
        rf"openPdfDownload\(\s*['\"]{rcp}['\"]\s*,\s*['\"](\d+)['\"]",
        # 목차 트리: nodeN['rcpNo'] = "접수번호"; nodeN['dcmNo'] = "문서번호";
        rf"\[['\"]rcpNo['\"]\]\s*=\s*['\"]{rcp}['\"]\s*;\s*\w+\[['\"]dcmNo['\"]\]\s*=\s*['\"](\d+)['\"]",
        # 본문 표시 함수: viewDoc('접수번호', '문서번호', ...)
        rf"viewDoc\(\s*['\"]{rcp}['\"]\s*,\s*['\"](\d+)['\"]",
        # 다운로드 화면 주소
        rf"rcp_no={rcp}&(?:amp;)?dcm_no=(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def fetch_dcm_no(
    rcept_no: str,
    session: requests.Session | None = None,
    timeout: int = 30,
) -> str:
    session = session or requests.Session()
    try:
        response = session.get(
            DART_VIEWER_URL,
            params={"rcpNo": rcept_no},
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise DartError(f"DART 공시 뷰어 요청에 실패했습니다: {error}") from None

    response.encoding = response.encoding or "utf-8"
    dcm_no = extract_dcm_no(response.text, rcept_no)
    if not dcm_no:
        raise DartError(
            "DART 공시 뷰어에서 PDF 문서번호(dcm_no)를 찾지 못했습니다. "
            f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no} 의 '다운로드' 창 주소에서 "
            "dcm_no를 확인해 companies.yaml의 official_pdf.dcm_no에 넣어 주세요."
        )
    return dcm_no


# ---------------------------------------------------------------------------
# 4. 재무제표 계정 금액
# ---------------------------------------------------------------------------

def fetch_financial_accounts(
    client: DartClient,
    corp_code: str,
    fiscal_year: int,
    statement_scope: str,
) -> list[dict]:
    """사업보고서 기준 연결(CFS) 또는 별도(OFS) 전체 재무제표 계정 목록."""

    fs_div = "CFS" if statement_scope == "consolidated" else "OFS"
    payload = client._get_json(
        FINANCIAL_ACCOUNTS_URL,
        {
            "crtfc_key": client.api_key,
            "corp_code": corp_code,
            "bsns_year": str(fiscal_year),
            "reprt_code": BUSINESS_REPORT_CODE,
            "fs_div": fs_div,
        },
    )
    if payload.get("status") == "013":
        return []
    return list(payload.get("list", []))
