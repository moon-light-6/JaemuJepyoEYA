"""사용자 요청 → 기업·사업연도·연결/별도·공시 확정.

기업은 companies.yaml이 아니라 OpenDART 전체 기업 목록에서 먼저 찾는다.
- 이름은 정규화 후 완전히 같은 것만 인정한다 ('에코프로머티' ≠ '에코프로').
- 같은 이름이 여러 개면 상장·적재 여부로 고르지 않고 후보를 돌려준다.
- 조건이 빠지면 추측하지 않고 되묻는다.
적재 여부는 여기서 보지 않는다 (data_source.py).
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date

from langchain_core.messages import HumanMessage, SystemMessage

from fin_research.ingestion.auto_resolve import (
    CorpInfo,
    choose_report,
    load_corp_codes,
    normalize_corp_name,
)
from fin_research.ingestion.dart_client import DartClient, DartError
from fin_research.ingestion.industry_codes import industry_division_name
from fin_research.llm.client import PROJECT_ROOT
from fin_research.llm.output_parser import OutputParseError, ainvoke_structured
from fin_research.llm.router import get_llm_for_role
from fin_research.research.nodes.clarify import ClarifyExtraction
from fin_research.research.nodes.common import load_prompt
from fin_research.retrieval.search_target import SearchTarget

# scripts/pipelines/run_pipeline_xml_v2.py와 같은 고유번호 캐시
CORP_CODE_CACHE = PROJECT_ROOT / "data" / "raw" / "dart" / "CORPCODE.xml"
COMPANY_URL = "https://opendart.fss.or.kr/api/company.json"
MAX_CANDIDATES = 20
SCOPE_TEXT = {"consolidated": "연결", "separate": "별도"}

# 종목코드(6자리, 숫자로 시작)·고유번호(8자리)
_CODE = re.compile(r"(?<![0-9A-Za-z])(\d{8}|\d[0-9A-Z]{5})(?![0-9A-Za-z])")


class ClarificationNeeded(Exception):
    """사용자에게 되물어야 할 때. missing: company / year / scope."""

    def __init__(self, question: str, missing: list[str], candidates: list[dict] | None = None):
        super().__init__(question)
        self.question, self.missing, self.candidates = question, missing, candidates or []


class DisclosureNotFound(DartError):
    """요청한 사업연도의 사업보고서가 없을 때."""


@dataclass(frozen=True)
class ResolvedRequest:
    """조사와 검증에 끝까지 같은 값으로 쓰는 확정 조건."""

    company: str
    stock_code: str
    target: SearchTarget
    report_name: str
    selection_reason: str
    focus: list[str] = field(default_factory=list)

    @property
    def company_config(self) -> dict:
        """companies.yaml 항목과 같은 형태 (실행 단위로만 사용)."""
        t = self.target
        return {
            "name": self.company,
            "corp_code": t.corp_code,
            "reports": {str(t.fiscal_year): {"fiscal_year": t.fiscal_year, "rcept_no": t.rcept_no}},
        }

    @property
    def graph_request(self) -> str:
        """기존 Research 그래프의 clarify에 넘길 요청. 이미 확정한 조건만 적는다."""
        t = self.target
        text = f"{self.company}의 {t.fiscal_year}년 {SCOPE_TEXT[t.statement_scope]} 기준 재무 분석 보고서를 작성해줘."
        return text + (f" 특히 {', '.join(self.focus)} 중심으로 분석해줘." if self.focus else "")

    def to_dict(self) -> dict:
        return {
            "company": self.company, "stock_code": self.stock_code, **asdict(self.target),
            "report_name": self.report_name, "selection_reason": self.selection_reason, "focus": self.focus,
        }


async def extract_conditions(request: str, budget=None) -> ClarifyExtraction:
    """기존 clarify의 프롬프트·스키마로 조건을 추출만 한다."""
    llm = get_llm_for_role("clarify", budget=budget)
    messages = [SystemMessage(load_prompt("clarify")), HumanMessage(f"사용자 요청:\n{request}")]
    try:
        return await ainvoke_structured(llm, messages, ClarifyExtraction)
    except OutputParseError:
        raise ClarificationNeeded(
            "요청을 이해하지 못했습니다. 기업명, 사업연도, 연결/별도 기준을 다시 알려주세요.",
            ["company", "year", "scope"],
        ) from None


# ─────────────────────────── 기업 ───────────────────────────


def match_corps(mention: str, corps: Iterable[CorpInfo]) -> list[CorpInfo]:
    """고유번호(8자리)·종목코드(6자리)·정규화한 이름이 '완전히 같은' 기업 전부."""
    mention = mention.strip()
    corps = list(corps)
    if re.fullmatch(r"\d{8}", mention):
        return [c for c in corps if c.corp_code == mention]
    if _CODE.fullmatch(mention.upper()):
        by_stock = [c for c in corps if c.stock_code.strip().upper() == mention.upper()]
        if by_stock:
            return by_stock
    key = normalize_corp_name(mention)
    return [c for c in corps if key and normalize_corp_name(c.corp_name) == key]


def similar_listed(mention: str, corps: Iterable[CorpInfo], limit: int = 8) -> list[CorpInfo]:
    """안내용 비슷한 상장사. 자동 선택에는 쓰지 않는다."""
    key = normalize_corp_name(mention)
    return [c for c in corps if c.is_listed and key and key in normalize_corp_name(c.corp_name)][:limit]


def describe(corps: list[CorpInfo], client: DartClient | None) -> list[dict]:
    """후보를 구분할 수 있는 정보. 기업개황 조회가 실패해도 코드 정보는 돌려준다."""
    out = []
    for corp in corps[:MAX_CANDIDATES]:
        item = {"corp_name": corp.corp_name, "corp_code": corp.corp_code, "stock_code": corp.stock_code.strip()}
        if client is not None:
            try:
                info = client._get_json(COMPANY_URL, {"crtfc_key": client.api_key, "corp_code": corp.corp_code})
                item["ceo"] = info.get("ceo_nm") or ""
                item["industry"] = industry_division_name(str(info.get("induty_code") or ""))
            except DartError:
                pass
        out.append(item)
    return out


def resolve_corp(request: str, extraction: ClarifyExtraction, corps: list[CorpInfo],
                 client: DartClient | None, corp_code: str | None = None) -> CorpInfo:
    """기업 하나를 확정한다. 확정할 수 없으면 ClarificationNeeded."""
    codes = [corp_code] if corp_code else list(dict.fromkeys(_CODE.findall(request)))
    mentions = list(dict.fromkeys(m.strip() for m in extraction.company_mentions if m.strip()))
    if not mentions:
        mentions = [c for c in codes if match_corps(c, corps)]
    if not mentions:
        raise ClarificationNeeded("어느 기업을 분석할까요? 정확한 기업명이나 종목코드를 알려주세요.", ["company"])

    resolved: dict[str, CorpInfo] = {}
    for mention in mentions:
        matches = match_corps(mention, corps)
        if not matches:
            similar = similar_listed(mention, corps)
            hint = "비슷한 이름의 상장사 후보를 참고해 정확한 기업명을 알려주세요." if similar else "정확한 기업명을 알려주세요."
            raise ClarificationNeeded(
                f"'{mention}'과(와) 이름이 정확히 같은 기업을 OpenDART에서 찾지 못했습니다. {hint}",
                ["company"], describe(similar, None),
            )
        if len(matches) > 1:  # 사용자가 코드를 함께 적었을 때만 좁힌다
            chosen = [c for c in matches if c.corp_code in codes or c.stock_code.strip() in codes]
            if len(chosen) != 1:
                raise ClarificationNeeded(
                    f"'{mention}'과(와) 이름이 같은 기업이 {len(matches)}개입니다. "
                    "고유번호(8자리)나 종목코드(6자리)를 함께 알려주세요.",
                    ["company"], describe(matches, client),
                )
            matches = chosen
        resolved[matches[0].corp_code] = matches[0]

    if len(resolved) > 1:
        raise ClarificationNeeded(
            "한 번에 한 기업만 분석할 수 있습니다. 어느 기업을 분석할까요?",
            ["company"], describe(list(resolved.values()), None),
        )
    corp = next(iter(resolved.values()))
    # 이름과 코드를 함께 적었는데 서로 다른 기업이면 어느 쪽도 고르지 않는다
    by_code = {c.corp_code: c for code in codes for c in match_corps(code, corps)}
    if (by_code or corp_code) and corp.corp_code not in by_code:
        raise ClarificationNeeded(
            f"요청의 기업명({corp.corp_name})과 기업 코드가 서로 다른 기업을 가리킵니다. 어느 기업을 분석할까요?",
            ["company"], describe([corp, *by_code.values()], None),
        )
    return corp


# ─────────────────────────── 연도·기준·공시 ───────────────────────────


def missing_conditions(extraction: ClarifyExtraction) -> tuple[list[str], list[str]]:
    """(빠진 조건, 물어볼 문장). 여러 값을 요청한 경우도 하나를 고르게 한다."""
    missing, questions = [], []
    if len(set(extraction.explicit_years)) != 1:
        missing.append("year")
        questions.append("한 번에 한 사업연도만 분석할 수 있습니다. 어느 사업연도인가요? (예: 2025년)"
                         if extraction.explicit_years else "어느 사업연도를 분석할까요? (예: 2025년)")
    if len(set(extraction.scopes)) != 1:
        missing.append("scope")
        questions.append("연결과 별도 중 하나의 기준만 선택해 주세요." if extraction.scopes
                         else "연결 기준과 별도 기준 중 어느 기준으로 분석할까요?")
    return missing, questions


def resolve_disclosure(client: DartClient, corp: CorpInfo, year: int):
    """사업연도 다음 해에 공시된 사업보고서 (기존 규칙: 정정공시가 있으면 최신 접수본)."""
    candidates = [] if not 1999 <= year <= date.today().year else client.search_business_reports(
        corp.corp_code, year, f"{year + 1}-01-01", f"{year + 1}-12-31")
    if not candidates:
        raise DisclosureNotFound(
            f"{corp.corp_name}({corp.corp_code})의 {year}년 사업보고서를 OpenDART에서 찾지 못했습니다. "
            "아직 공시되지 않았거나 제출 대상이 아닐 수 있습니다."
        )
    return choose_report(candidates)


async def resolve_request(request: str, *, client: DartClient, budget=None, corp_code: str | None = None,
                          extract=extract_conditions, load_corps=None) -> ResolvedRequest:
    """자연어 요청 하나 → ResolvedRequest."""
    if not request.strip():
        raise ClarificationNeeded("분석할 기업명, 사업연도, 연결/별도 기준을 알려주세요.", ["company", "year", "scope"])
    extraction = await extract(request, budget)
    corps = await asyncio.to_thread(load_corps or (lambda: load_corp_codes(client, CORP_CODE_CACHE)))
    missing, questions = missing_conditions(extraction)
    try:
        corp = await asyncio.to_thread(resolve_corp, request, extraction, corps, client, corp_code)
    except ClarificationNeeded as e:  # 기업과 함께 다른 조건도 한 번에 묻는다
        raise ClarificationNeeded(" ".join([e.question, *questions]), [*e.missing, *missing], e.candidates) from None
    if missing:
        raise ClarificationNeeded(" ".join(questions), missing)

    year = extraction.explicit_years[0]
    report, reason = await asyncio.to_thread(resolve_disclosure, client, corp, year)
    return ResolvedRequest(
        company=corp.corp_name, stock_code=corp.stock_code.strip(),
        target=SearchTarget(corp.corp_code, year, extraction.scopes[0], report.rcept_no),
        report_name=report.report_name, selection_reason=reason, focus=list(extraction.focus),
    )
