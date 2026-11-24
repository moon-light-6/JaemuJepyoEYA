"""감사보고서 XML 검색용 문서·청킹 생성 테스트."""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from fin_research.ingestion.xml_documents import build_xml_documents
from scripts.processing.build_xml_chunks import build_xml_chunks


def audit_xml(document_name: str, prefix: str) -> str:
    return f"""<?xml version=\"1.0\" encoding=\"utf-8\"?>
<DOCUMENT>
  <DOCUMENT-NAME>{document_name}</DOCUMENT-NAME>
  <BODY>
    <SECTION-1>
      <TITLE ATOC=\"Y\">(첨부) {prefix}재 무 제 표</TITLE>
      <TABLE><TR><TD>재무상태표</TD></TR></TABLE>
      <TABLE><TR><TD>자산총계</TD><TD>1,234,567</TD></TR></TABLE>
      <TABLE><TR><TD>현금흐름표</TD></TR></TABLE>
      <TABLE><TR><TD>영업활동현금흐름</TD><TD>765,432</TD></TR></TABLE>
      <SECTION-2>
        <TITLE ATOC=\"Y\">주석</TITLE>
        <P>1. 회사의 개요</P>
        <P>테스트 회사의 본사입니다.</P>
        <P>2. 회계정책</P>
        <TABLE><TR><TD>기능통화</TD><TD>원화</TD></TR></TABLE>
      </SECTION-2>
    </SECTION-1>
  </BODY>
</DOCUMENT>"""


class XmlDocumentTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.zip_path = Path(self.directory.name) / "sample.zip"
        with zipfile.ZipFile(self.zip_path, "w") as archive:
            archive.writestr("sample.xml", "<DOCUMENT><DOCUMENT-NAME>사업보고서</DOCUMENT-NAME><P>&</P></DOCUMENT>")
            archive.writestr("sample_00760.xml", audit_xml("감사보고서", ""))
            archive.writestr("sample_00761.xml", audit_xml("연결감사보고서", "연 결 "))

    def tearDown(self):
        self.directory.cleanup()

    def test_separate_selects_audit_xml_and_preserves_xpath(self):
        records, metadata = build_xml_documents(
            self.zip_path, "00258689", 2025, "separate", "JYP Ent."
        )
        self.assertEqual(metadata["source_xml"], "sample_00760.xml")
        self.assertEqual(metadata["financial_statement_documents"], 2)
        self.assertEqual(metadata["note_documents"], 2)
        self.assertTrue(all(item["source_format"] == "xml" for item in records))
        self.assertTrue(all(item["source_xpath"].startswith("/DOCUMENT") for item in records))
        self.assertIn("1,234,567", records[0]["text"])

    def test_consolidated_selects_consolidated_audit_xml(self):
        records, metadata = build_xml_documents(
            self.zip_path, "00258689", 2025, "consolidated", "JYP Ent."
        )
        self.assertEqual(metadata["source_xml"], "sample_00761.xml")
        self.assertEqual(records[0]["section"], "연결재무제표")

    def test_xml_chunks_keep_xml_source_metadata(self):
        records, _ = build_xml_documents(
            self.zip_path, "00258689", 2025, "separate", "JYP Ent."
        )
        chunks = build_xml_chunks(records, chunk_size=80, chunk_overlap=10)
        # 짧은 표/주석은 하나의 청크로 유지될 수 있으므로, 분할 개수 자체보다
        # XML 출처 메타데이터가 모든 청크에 보존되는지를 확인한다.
        self.assertGreaterEqual(len(chunks), len(records))
        self.assertTrue(all(item["source_format"] == "xml" for item in chunks))
        self.assertTrue(all(item["source_file"] == "sample_00760.xml" for item in chunks))

    def test_audit_report_preferred_over_business_report(self):
        _, metadata = build_xml_documents(self.zip_path, "00258689", 2025, "consolidated", "JYP Ent.")
        self.assertEqual(metadata["source_document"], "감사보고서")


def paragraph_title_audit_xml() -> str:
    """재무제표 제목이 TABLE이 아니라 P인 연결감사보고서."""

    return """<?xml version=\"1.0\" encoding=\"utf-8\"?>
<DOCUMENT>
  <DOCUMENT-NAME>연결감사보고서</DOCUMENT-NAME>
  <BODY>
    <SECTION-1>
      <TITLE ATOC=\"Y\">(첨부)연 결 재 무 제 표</TITLE>
      <TABLE><TR><TD>회사와 그 종속기업</TD></TR></TABLE>
      <P>연 결 재 무 상 태 표</P>
      <TABLE><TR><TD>자산총계</TD><TD>1,234,567</TD></TR></TABLE>
      <TABLE><TR><TD>별첨 주석 참조</TD></TR></TABLE>
      <P>연 결 현 금 흐 름 표</P>
      <TABLE><TR><TD>영업활동으로 인한 현금흐름</TD><TD>(765,432)</TD></TR></TABLE>
      <P>본 재무제표는 재무상태표와 현금흐름표를 포함하며 별첨 주석과 함께 읽어야 합니다.</P>
      <SECTION-2>
        <TITLE ATOC=\"Y\">주석</TITLE>
        <P>1. 회사의 개요</P>
        <P>테스트 회사의 본사입니다.</P>
      </SECTION-2>
    </SECTION-1>
  </BODY>
</DOCUMENT>"""


class ParagraphTitleStatementTest(unittest.TestCase):
    def test_statements_with_paragraph_titles_are_documented(self):
        with tempfile.TemporaryDirectory() as directory:
            zip_path = Path(directory) / "sample.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("sample_00761.xml", paragraph_title_audit_xml())
            records, metadata = build_xml_documents(zip_path, "00164742", 2025, "consolidated", "테스트")

        statements = [item for item in records if item["document_type"] == "xml_financial_statement"]
        self.assertEqual(metadata["financial_statement_documents"], 2)
        self.assertEqual([item["title"] for item in statements], ["재무상태표", "현금흐름표"])
        self.assertIn("(765,432)", statements[1]["text"])
        # 재무제표를 언급하는 긴 설명 문단(P)은 제목으로 오인되어 문서가 늘어나지 않는다(2건 유지).
        # 문서 위치는 제목 P의 XPath다.
        self.assertTrue(statements[1]["source_xpath"].endswith("/P[2]"))
        self.assertEqual(metadata["note_documents"], 1)


def business_report_xml() -> str:
    """기재정정 사업보고서처럼 본문만 있는 XML. TABLE-GROUP 하나가 재무제표·주석 하나."""
    return """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT>
  <DOCUMENT-NAME>사업보고서</DOCUMENT-NAME>
  <BODY>
    <SECTION-1>
      <TITLE>III. 재무에 관한 사항</TITLE>
      <SECTION-2><TITLE>1. 요약재무정보</TITLE><P>요약</P></SECTION-2>
      <LIBRARY>
        <SECTION-2>
          <TITLE>2. 연결재무제표</TITLE>
          <TABLE-GROUP>
            <TITLE>2-1. 연결 재무상태표</TITLE>
            <TABLE><TR><TE>자산총계</TE><TE ACODE="ifrs-full_Assets">797,923,033</TE></TR></TABLE>
          </TABLE-GROUP>
          <P></P>
          <TABLE-GROUP>
            <TITLE>2-4. 연결 현금흐름표</TITLE>
            <TABLE><TR><TD>영업활동현금흐름</TD><TD>1,234</TD></TR></TABLE>
          </TABLE-GROUP>
        </SECTION-2>
        <SECTION-2>
          <TITLE>3. 연결재무제표 주석</TITLE>
          <TABLE-GROUP>
            <TITLE>1. 지배기업의 개요 (연결)</TITLE>
            <TABLE><TR><TH ENG="" KB Insurance Co., Ltd "">KB손해보험</TH></TR></TABLE>
          </TABLE-GROUP>
        </SECTION-2>
        <SECTION-2>
          <TITLE>4. 재무제표</TITLE>
          <TABLE-GROUP>
            <TITLE>4-1. 재무상태표</TITLE>
            <TABLE><TR><TD>자산총계</TD><TD>55,555</TD></TR></TABLE>
          </TABLE-GROUP>
        </SECTION-2>
        <SECTION-2>
          <TITLE>5. 재무제표 주석</TITLE>
          <TABLE-GROUP><TITLE>1. 회사의 개요</TITLE><TABLE><TR><TD>본사</TD></TR></TABLE></TABLE-GROUP>
        </SECTION-2>
      </LIBRARY>
    </SECTION-1>
  </BODY>
</DOCUMENT>"""


class BusinessReportFallbackTest(unittest.TestCase):
    """감사보고서 첨부가 없는 공시는 사업보고서 본문에서 같은 형식의 문서를 만든다."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.zip_path = Path(self.directory.name) / "20260619000667.zip"
        with zipfile.ZipFile(self.zip_path, "w") as archive:
            archive.writestr("20260619000667.xml", business_report_xml())

    def tearDown(self):
        self.directory.cleanup()

    def test_consolidated_reads_statements_and_notes_from_body(self):
        records, metadata = build_xml_documents(self.zip_path, "00688996", 2025, "consolidated", "KB금융")
        self.assertEqual(metadata["source_document"], "사업보고서")
        self.assertEqual(metadata["financial_statement_documents"], 2)
        self.assertEqual(metadata["note_documents"], 1)
        self.assertEqual([r["title"] for r in records[:2]], ["재무상태표", "현금흐름표"])
        self.assertIn("797,923,033", records[0]["text"])
        self.assertTrue(records[0]["source_xpath"].endswith("/LIBRARY[1]/SECTION-2[1]/TABLE-GROUP[1]"))
        self.assertEqual(records[0]["section"], "연결재무제표")
        self.assertEqual(records[2]["document_type"], "xml_note")

    def test_separate_reads_separate_sections(self):
        records, _ = build_xml_documents(self.zip_path, "00688996", 2025, "separate", "KB금융")
        self.assertEqual([r["title"] for r in records], ["재무상태표", "1. 회사의 개요"])
        self.assertIn("55,555", records[0]["text"])

    def test_broken_attribute_quotes_are_repaired(self):
        records, metadata = build_xml_documents(self.zip_path, "00688996", 2025, "consolidated", "KB금융")
        self.assertEqual(metadata["normalization"]["repaired_broken_attributes"], 1)
        self.assertIn("KB손해보험", records[2]["text"])


if __name__ == "__main__":
    unittest.main()
