"""사업보고서 본문과 기준별 감사보고서를 출처별 문서로 추출한다."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from fin_research.ingestion.report_parser import build_locations, compact_text, normalize_xml_text, normalized_label
from fin_research.ingestion.xml_documents import (
    XmlDocumentError, build_xml_documents, element_text, repair_broken_attributes,
)


def _business_root(path: Path):
    failures = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.lower().endswith('.xml'):
                continue
            try:
                raw = archive.read(name).decode('utf-8')
                prepared, stats = normalize_xml_text(raw)
                prepared, repaired = repair_broken_attributes(prepared)
                root = ElementTree.fromstring(prepared)
            except (UnicodeDecodeError, ElementTree.ParseError) as exc:
                failures.append(f'{name}: {exc}')
                continue
            title = root.find('DOCUMENT-NAME')
            if title is not None and normalized_label(compact_text(title)) == '사업보고서':
                return name, root, {**stats, 'repaired_broken_attributes': repaired}
    raise XmlDocumentError('사업보고서 본문 XML을 찾거나 읽지 못했습니다. ' + '; '.join(failures))


def build_xml_documents_v3(zip_path: Path, corp_code: str, fiscal_year: int, scope: str, company: str):
    """본문의 텍스트와 표를 순서대로 수집하며 감사보고서 기존 추출을 보존한다."""
    filename, root, normalization = _business_root(zip_path)
    body = root.find('BODY')
    if body is None:
        raise XmlDocumentError('사업보고서 본문 BODY가 없습니다.')
    audit, audit_meta = build_xml_documents(zip_path, corp_code, fiscal_year, scope, company)
    # 기존 함수의 추가 문맥은 본문 전체 추출과 중복된다.
    audit = [item for item in audit if item['document_type'] in ('xml_financial_statement', 'xml_note')]
    for item in audit:
        item['source_document_type'] = 'business_report' if audit_meta['source_document'] == '사업보고서' else 'audit_report'
        item['content_scope'] = scope
    _, locations = build_locations(root)
    records = []
    ignored = {'COLGROUP', 'COL', 'TITLE'}

    def visit(node, headings, content_scope):
        tag = str(node.tag)
        if re.fullmatch(r'SECTION-\d+', tag):
            title = node.find('TITLE')
            heading = compact_text(title) if title is not None else ''
            if heading:
                headings = [*headings, heading]
                label = normalized_label(heading)
                if '연결재무제표' in label or '연결재무상태표' in label:
                    content_scope = 'consolidated'
                elif re.search(r'(?<!연결)재무제표', label):
                    content_scope = 'separate'
        if tag in ('P', 'TABLE', 'COVER-TITLE'):
            value = element_text(node)
            if value:
                location = locations[id(node)]
                sequence = len(records) + 1
                records.append({
                    'page_id': f'{corp_code}_{fiscal_year}_{scope}_v3_body_{sequence:06d}',
                    'text': value, 'company': company, 'fiscal_year': fiscal_year,
                    'statement_scope': scope, 'content_scope': content_scope,
                    'document_type': 'xml_business_report', 'source_document_type': 'business_report',
                    'source_format': 'xml', 'source_file': filename,
                    'source_xpath': location['xpath'], 'source_element_index': location['element_index'],
                    'section': ' > '.join(headings[:-1]) or (headings[0] if headings else '사업보고서'),
                    'title': headings[-1] if headings else '사업보고서',
                })
            return
        if tag in ignored:
            return
        for child in node:
            visit(child, headings, content_scope)

    visit(body, [], 'unknown')
    if not records:
        raise XmlDocumentError('사업보고서 본문에서 문단이나 표를 추출하지 못했습니다.')
    return audit + records, {
        'version': 3, 'business_report_xml': filename,
        'business_normalization': normalization, 'audit': audit_meta,
        'business_documents': len(records), 'audit_documents': len(audit),
        'scope_note': 'unknown은 판별하지 않은 내용이며 공통으로 확정한 것이 아닙니다.',
        'coverage_note': 'P, TABLE, COVER-TITLE 텍스트 대상. 이미지 및 기타 표현은 별도 확인 필요.',
    }
