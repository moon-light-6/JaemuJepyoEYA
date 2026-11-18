"""OpenDART 계정값과 XML 문서 위치를 연결해 검토용 평가셋을 만든다.

자동 생성 결과는 모두 ``draft``다. 실제 XML 본문·XPath를 사람이 대조한 뒤에만
``verified``로 바꾸며, 이 모듈은 LLM API를 사용하지 않는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evaluation.auto_dataset import (
    ACCOUNT_SPECS,
    COMPARISON_ORDER,
    NUMERIC_ORDER,
    SCOPE_LABEL,
    display_value,
    parse_amount,
    select_account,
)
from fin_research.ingestion.section_detector import UNIT_SCALE


ANNOTATION_METHOD = "auto_opendart_xml_xpath_candidate"


def _key(value: object) -> str:
    return re.sub(r"[\s|ㆍ·(),]", "", str(value or "")).lower()


def _detect_unit(documents: list[dict[str, Any]]) -> str:
    text = "\n".join(str(document.get("text", "")) for document in documents[:8])
    for unit in ("백만원", "천원", "원"):
        if unit in text:
            return unit
    # DART 감사보고서 재무제표의 기본 표시 단위. 확정값이 아니라 draft 검토용이다.
    return "백만원"


def _candidate(
    documents: list[dict[str, Any]], names: tuple[str, ...], values: list[str]
) -> dict[str, Any] | None:
    name_keys = [_key(name) for name in names if name]
    value_keys = [_key(value) for value in values if value]
    for document in documents:
        text_key = _key(document.get("text"))
        if any(name in text_key for name in name_keys) and all(value in text_key for value in value_keys):
            return document
    return None


def _source(document: dict[str, Any]) -> dict[str, str]:
    return {
        "source_file": str(document["source_file"]),
        "source_xpath": str(document["source_xpath"]),
    }


def _account_info(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("sj_div", "account_id", "account_nm")}


def build_xml_draft_dataset(
    accounts: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    fiscal_year: int,
    statement_scope: str,
    max_questions: int = 20,
) -> tuple[list[dict[str, Any]], list[str]]:
    """XML 본문에서 계정명·표시금액이 함께 확인된 질문 후보를 만든다."""

    if statement_scope not in SCOPE_LABEL:
        raise ValueError("statement_scope는 consolidated 또는 separate여야 합니다.")
    if max_questions <= 0:
        raise ValueError("max_questions는 1 이상이어야 합니다.")
    if not documents:
        raise ValueError("XML 문서가 비어 있습니다.")

    unit = _detect_unit(documents)
    scope = SCOPE_LABEL[statement_scope]
    items: list[dict[str, Any]] = []
    skipped: list[str] = []

    def append(item: dict[str, Any]) -> None:
        item["id"] = f"X{len(items) + 1:02d}"
        item["annotation_status"] = "draft"
        item["annotation_method"] = ANNOTATION_METHOD
        items.append(item)

    for key in NUMERIC_ORDER:
        if len(items) >= max_questions:
            break
        spec = ACCOUNT_SPECS[key]
        row = select_account(accounts, spec)
        amount = parse_amount(row.get("thstrm_amount")) if row else None
        if amount is None:
            skipped.append(f"{spec.label[:-1]}: OpenDART 당기값 없음")
            continue
        expected = display_value(amount, unit)
        names = tuple(spec.names) + ((row.get("account_nm") or ""),)
        document = _candidate(documents, names, [expected])
        if document is None:
            skipped.append(f"{spec.label[:-1]}: XML에서 계정명·당기값을 함께 찾지 못함")
            continue
        source = _source(document)
        append({
            "question": f"{fiscal_year}년 {scope} 기준 {spec.label} 얼마인가?",
            "answer_type": "numeric",
            "expected_value": expected,
            "unit": unit,
            "gold_sources": [source],
            "candidate_sources": [{**source, "title": document.get("title", ""), "match_reason": "계정명과 당기 금액이 같은 XML 문서"}],
            "category": spec.category,
            "opendart_account": _account_info(row),
        })

    for key in COMPARISON_ORDER:
        if len(items) >= max_questions:
            break
        spec = ACCOUNT_SPECS[key]
        row = select_account(accounts, spec)
        current = parse_amount(row.get("thstrm_amount")) if row else None
        previous = parse_amount(row.get("frmtrm_amount")) if row else None
        if current is None or previous is None:
            continue
        current_text, previous_text = display_value(current, unit), display_value(previous, unit)
        names = tuple(spec.names) + ((row.get("account_nm") or ""),)
        document = _candidate(documents, names, [current_text, previous_text])
        if document is None:
            continue
        source = _source(document)
        append({
            "question": f"{fiscal_year}년과 {fiscal_year - 1}년 {scope} 기준 {spec.label} 각각 얼마인가?",
            "answer_type": "comparison",
            "expected_value": {str(fiscal_year): current_text, str(fiscal_year - 1): previous_text},
            "unit": unit,
            "gold_sources": [source],
            "candidate_sources": [{**source, "title": document.get("title", ""), "match_reason": "계정명과 당기·전기 금액이 같은 XML 문서"}],
            "category": spec.category,
            "opendart_account": _account_info(row),
        })

    return items, skipped


def save_xml_draft_dataset(path: str | Path, items: list[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
