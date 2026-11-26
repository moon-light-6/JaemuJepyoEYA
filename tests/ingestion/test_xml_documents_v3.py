import tempfile
import zipfile
import sys
from pathlib import Path

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from fin_research.ingestion.xml_documents import XmlDocumentError
from fin_research.ingestion.xml_documents_v3 import build_xml_documents_v3
from scripts.processing.build_xml_chunks_v3 import build_xml_chunks


def test_body_and_selected_audit_keep_sources_and_scope():
    body = '''<DOCUMENT><DOCUMENT-NAME>사업보고서</DOCUMENT-NAME><BODY>
    <SECTION-1><TITLE>사업의 내용</TITLE><P>사업 설명</P>
    <SECTION-2><TITLE>연결재무제표</TITLE><TABLE><TR><TD>연결자산</TD><TD>100</TD></TR></TABLE></SECTION-2>
    <SECTION-2><TITLE>재무제표</TITLE><P>별도 자산</P></SECTION-2></SECTION-1></BODY></DOCUMENT>'''
    def audit(name):
        return f'''<DOCUMENT><DOCUMENT-NAME>{name}</DOCUMENT-NAME><BODY><SECTION-1>
        <TITLE>(첨부) 재무제표</TITLE><TABLE><TR><TD>재무상태표</TD></TR></TABLE>
        <TABLE><TR><TD>자산총계</TD><TD>100</TD></TR></TABLE>
        <SECTION-2><TITLE>주석</TITLE><P>1. 회사의 개요</P><P>주석 내용</P></SECTION-2>
        </SECTION-1></BODY></DOCUMENT>'''
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'sample.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('main.xml', body)
            archive.writestr('a.xml', audit('감사보고서'))
            archive.writestr('b.xml', audit('연결감사보고서'))
        for scope, selected in [('consolidated', 'b.xml'), ('separate', 'a.xml')]:
            docs, meta = build_xml_documents_v3(path, '12345678', 2025, scope, '테스트')
            assert meta['business_report_xml'] == 'main.xml'
            assert meta['audit']['source_xml'] == selected
            body_docs = [d for d in docs if d['source_file'] == 'main.xml']
            assert [d['content_scope'] for d in body_docs] == ['unknown', 'consolidated', 'separate']
            assert '연결자산 | 100' in body_docs[1]['text']
            chunks = build_xml_chunks(docs, 600, 100)
            assert {c['source_file'] for c in chunks} == {'main.xml', selected}
            assert all(c['source_xpath'] and 'content_scope' in c for c in chunks)


def test_missing_business_report_fails():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'sample.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('audit.xml', '<DOCUMENT><DOCUMENT-NAME>감사보고서</DOCUMENT-NAME></DOCUMENT>')
        with unittest.TestCase().assertRaisesRegex(XmlDocumentError, '사업보고서 본문'):
            build_xml_documents_v3(path, '12345678', 2025, 'separate', '테스트')


class XmlDocumentsV3Test(unittest.TestCase):
    def test_sources_and_scopes(self):
        test_body_and_selected_audit_keep_sources_and_scope()

    def test_missing_business_report(self):
        test_missing_business_report_fails()
