"""감사보고서 XML 검색용 문서·청킹 생성 테스트."""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from fin_research.ingestion.xml_documents import build_xml_documents
from scripts.build_xml_chunks import build_xml_chunks


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


if __name__ == "__main__":
    unittest.main()
