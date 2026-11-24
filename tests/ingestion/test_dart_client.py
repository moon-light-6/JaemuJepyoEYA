# [OpenDART 수집 클라이언트의 네트워크 없는 기본 테스트]

from __future__ import annotations
from pathlib import Path

import io
import json
import sys
import tempfile
import unittest
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.ingestion.dart_client import AmbiguousReportError, DartClient, ReportCandidate


class FakeResponse:
    def __init__(self, payload: dict | None = None, content: bytes = b""):
        self.payload = payload
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload or {}

    def iter_content(self, chunk_size: int):
        yield self.content


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def candidate(rcept_no: str) -> ReportCandidate:
    return ReportCandidate("삼성전자", "00126380", 2025, "사업보고서 (2025.12)", rcept_no, "20260318", "삼성전자", "")


class DartClientTest(unittest.TestCase):
    def test_search_keeps_correction_candidates(self):
        session = FakeSession(FakeResponse({"status": "000", "list": [
            {"corp_name": "삼성전자", "corp_code": "00126380", "report_nm": "사업보고서 (2025.12)", "rcept_no": "1", "rcept_dt": "20260318", "flr_nm": "삼성전자", "rm": ""},
            {"corp_name": "삼성전자", "corp_code": "00126380", "report_nm": "[정정]사업보고서 (2025.12)", "rcept_no": "2", "rcept_dt": "20260320", "flr_nm": "삼성전자", "rm": ""},
        ]}))
        results = DartClient("key", session=session).search_business_reports("00126380", 2025, "2026-01-01", "2026-12-31")
        self.assertEqual([item.rcept_no for item in results], ["1", "2"])
        self.assertEqual(session.calls[0][1]["params"]["bgn_de"], "20260101")

    def test_multiple_candidates_need_configured_receipt_number(self):
        client = DartClient("key")
        with self.assertRaises(AmbiguousReportError):
            client.resolve_report([candidate("1"), candidate("2")])
        self.assertEqual(client.resolve_report([candidate("1"), candidate("2")], "2").rcept_no, "2")

    def test_download_valid_zip_and_write_metadata(self):
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, "w") as archive:
            archive.writestr("report.xml", "<report />")
        session = FakeSession(FakeResponse(content=memory_file.getvalue()))
        client = DartClient("key", session=session)
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / "source.zip"
            client.download_original_zip("20260318000001", zip_path)
            metadata_path = client.save_metadata(candidate("20260318000001"), zip_path)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertTrue(zipfile.is_zipfile(zip_path))
            self.assertTrue(metadata["zip_valid"])
            self.assertFalse(metadata["financial_statements_and_notes_verified"])
            client.download_original_zip("20260318000001", zip_path)
            self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
