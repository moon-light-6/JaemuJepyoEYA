# [기업 자동 처리(run_pipeline)용 모듈의 네트워크 없는 테스트]

from __future__ import annotations

import json
import sys
import unittest
import csv
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evaluation.auto_dataset import (
    ANNOTATION_METHOD,
    build_auto_eval_set,
    parse_amount,
    scaled_digits,
)
from fin_research.ingestion.auto_resolve import (
    CorpInfo,
    CorpLookupError,
    choose_report,
    extract_dcm_no,
    find_corp,
    parse_corp_code_xml,
)
from fin_research.ingestion.dart_client import DartError, ReportCandidate
from fin_research.ingestion.section_detector import (
    SectionDetectionError,
    detect_sections,
)
from scripts.pipelines.run_pipeline import SUMMARY_FIELDS, save_summary


CORP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list><corp_code>00970453</corp_code><corp_name>파마리서치</corp_name><stock_code>214450</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>00000001</corp_code><corp_name>파마리서치</corp_name><stock_code> </stock_code><modify_date>20200101</modify_date></list>
  <list><corp_code>00199252</corp_code><corp_name>에이치엘비</corp_name><stock_code>028300</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>00258689</corp_code><corp_name>JYP Ent.</corp_name><stock_code>035900</stock_code><modify_date>20260101</modify_date></list>
</result>""".encode("utf-8")


def candidate(rcept_no: str, name: str = "사업보고서 (2025.12)") -> ReportCandidate:
    return ReportCandidate(
        company_name="테스트", corp_code="00970453", fiscal_year=2025, report_name=name,
        rcept_no=rcept_no, rcept_date=rcept_no[:8], filer_name="테스트", remarks="",
    )


class CorpLookupTest(unittest.TestCase):
    def setUp(self):
        self.corps = parse_corp_code_xml(CORP_XML)

    def test_name_prefers_listed_company(self):
        self.assertEqual(find_corp("파마리서치", self.corps).corp_code, "00970453")

    def test_stock_code_and_corp_code(self):
        self.assertEqual(find_corp("028300", self.corps).corp_name, "에이치엘비")
        self.assertEqual(find_corp("00258689", self.corps).stock_code, "035900")

    def test_normalized_name(self):
        self.assertEqual(find_corp("jyp ent", self.corps).corp_code, "00258689")

    def test_unknown_name_suggests_similar(self):
        with self.assertRaises(CorpLookupError) as context:
            find_corp("파마", self.corps)
        self.assertIn("파마리서치", str(context.exception))

    def test_ambiguous_listed_names_stop(self):
        corps = [CorpInfo("00000010", "동명", "111111"), CorpInfo("00000011", "동명", "222222")]
        with self.assertRaises(CorpLookupError):
            find_corp("동명", corps)


class ReportChoiceTest(unittest.TestCase):
    def test_single_candidate(self):
        report, _ = choose_report([candidate("20260318000001")])
        self.assertEqual(report.rcept_no, "20260318000001")

    def test_correction_uses_latest_by_default(self):
        candidates = [candidate("20260318000001"), candidate("20260410000002", "[기재정정]사업보고서 (2025.12)")]
        self.assertEqual(choose_report(candidates)[0].rcept_no, "20260410000002")
        self.assertEqual(choose_report(candidates, prefer="original")[0].rcept_no, "20260318000001")

    def test_configured_rcept_no_wins(self):
        candidates = [candidate("20260318000001"), candidate("20260410000002")]
        self.assertEqual(choose_report(candidates, "20260318000001")[0].rcept_no, "20260318000001")
        with self.assertRaises(DartError):
            choose_report(candidates, "20990101000000")


class SummarySaveTest(unittest.TestCase):
    def test_existing_csv_string_year_and_new_integer_year_are_sorted(self):
        """기존 CSV 문자열과 이번 실행 정수 연도가 섞여도 요약 저장이 성공한다."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS)
                writer.writeheader()
                writer.writerow({
                    "company": "JYP Ent.", "corp_code": "00258689",
                    "fiscal_year": "2025", "statement_scope": "consolidated",
                    "rcept_no": "20260318001519",
                })

            save_summary([{
                "company": "JYP Ent.", "corp_code": "00258689",
                "fiscal_year": 2025, "statement_scope": "separate",
                "rcept_no": "20260318001519",
            }], path)

            with path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["fiscal_year"] == "2025" for row in rows))


class DcmNoTest(unittest.TestCase):
    def test_download_button(self):
        html = "<button onclick=\"openPdfDownload('20260310002820', '11104488'); return false;\">"
        self.assertEqual(extract_dcm_no(html, "20260310002820"), "11104488")

    def test_tree_node(self):
        html = """node1['rcpNo'] = "20260323001673";\n\t\tnode1['dcmNo'] = "11169561";"""
        self.assertEqual(extract_dcm_no(html, "20260323001673"), "11169561")

    def test_view_doc_and_other_report_ignored(self):
        html = "viewDoc('20990101000000', '1', null); viewDoc('20260318001612', '11144186', null);"
        self.assertEqual(extract_dcm_no(html, "20260318001612"), "11144186")
        self.assertIsNone(extract_dcm_no("<html></html>", "20260318001612"))


def synthetic_pages(consolidated: bool = True) -> list[dict]:
    texts = {
        1: "목                 차\n2. 연결재무제표..................83\n3. 연결재무제표 주석.........91\n4. 재무제표........197",
        10: "III. 재무에 관한 사항\n1. 요약재무정보",
        11: "2. 연결재무제표\n2-1. 연결 재무상태표\n(단위 : 원)\n자산총계\n1,234,567,890\n1,000,000,000\n900,000,000",
        12: "2-2. 연결 포괄손익계산서\n매출액\n500,000,000\n400,000,000",
        13: "3. 연결재무제표 주석\n1. 일반사항 (연결)\n본문",
        14: "2. 재고자산 (연결)\n본문",
        15: "  \n전자공시시스템 dart.fss.or.kr\nPage 15\n",
        16: "4. 재무제표\n4-1. 재무상태표\n(단위 : 원)\n자산총계\n700,000,000\n600,000,000",
        17: "5. 재무제표 주석\n1. 일반사항\n본문",
        18: "본문",
        19: "6. 배당에 관한 사항\n본문",
    }
    if not consolidated:
        texts[11] = "2. 연결재무제표\n해당사항 없음"
        texts[13] = "3. 연결재무제표 주석\n해당사항 없음"
    return [{"page_number": number, "text": text} for number, text in sorted(texts.items())]


class SectionDetectorTest(unittest.TestCase):
    def test_consolidated_ranges_skip_toc_and_footer_page(self):
        plan = detect_sections(synthetic_pages())
        self.assertEqual(plan.statement_scope, "consolidated")
        self.assertEqual([r.as_argument() for r in plan.ranges], ["11-12:연결재무제표", "13-14:연결재무제표 주석"])
        self.assertEqual(plan.unit, "원")
        self.assertEqual(plan.statement_pages["BS"], [11, 12])

    def test_falls_back_to_separate_when_no_consolidated(self):
        plan = detect_sections(synthetic_pages(consolidated=False))
        self.assertEqual(plan.statement_scope, "separate")
        self.assertEqual([r.as_argument() for r in plan.ranges], ["16-16:재무제표", "17-18:재무제표 주석"])

    def test_forced_scope_and_failure(self):
        self.assertEqual(detect_sections(synthetic_pages(), "separate").statement_scope, "separate")
        with self.assertRaises(SectionDetectionError):
            detect_sections([{"page_number": 1, "text": "본문"}])


class AutoDatasetTest(unittest.TestCase):
    def test_amount_helpers(self):
        self.assertEqual(parse_amount("-1,234"), -1234)
        self.assertIsNone(parse_amount(""))
        self.assertEqual(scaled_digits(566_942_110_000_000, "백만원"), "566,942,110")
        self.assertIsNone(scaled_digits(999_000_000, "백만원"))

    def test_gold_page_requires_matching_label(self):
        pages = synthetic_pages()
        page_map = {page["page_number"]: page["text"] for page in pages}
        # 12쪽에 같은 숫자가 다른 계정명으로 있어도 Gold page로 쓰지 않는다.
        page_map[12] += "\n부채와자본총계\n1,234,567,890"
        plan = detect_sections(pages)
        accounts = [
            {"sj_div": "BS", "account_id": "ifrs-full_Assets", "account_nm": "자산총계",
             "thstrm_amount": "1234567890", "frmtrm_amount": "1000000000"},
            {"sj_div": "CIS", "account_id": "ifrs-full_Revenue", "account_nm": "매출액",
             "thstrm_amount": "500000000", "frmtrm_amount": "400000000"},
            {"sj_div": "BS", "account_id": "ifrs-full_Inventories", "account_nm": "재고자산",
             "thstrm_amount": "777777777", "frmtrm_amount": ""},
        ]
        items, skipped = build_auto_eval_set(accounts, page_map, plan, "x.pdf", 2025)
        by_question = {item["question"]: item for item in items}
        assets = by_question["2025년 연결 기준 자산총계는 얼마인가?"]
        self.assertEqual(assets["gold_sources"], [{"source_file": "x.pdf", "pdf_page": 11}])
        self.assertEqual(assets["annotation_method"], ANNOTATION_METHOD)
        self.assertIn("2025년과 2024년 연결 기준 매출액은 각각 얼마인가?", by_question)
        self.assertTrue(any("재고자산" in reason for reason in skipped))


REAL_REPORTS = {
    # corp_code, rcept_no: 사람이 정한 범위
    ("00126380", "20260310002820"): ["83-90:연결재무제표", "91-195:연결재무제표 주석"],
    ("00199252", "20260323001673"): ["97-101:연결재무제표", "102-212:연결재무제표 주석"],
    ("00536541", "20260318001612"): ["91-96:연결재무제표", "97-201:연결재무제표 주석"],
}


def real_pages(corp_code: str, rcept_no: str) -> list[dict] | None:
    path = PROJECT_ROOT / "data" / "processed" / "dart" / corp_code / "2025" / f"{rcept_no}_pdf_pages.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["pages"]


class RealReportTest(unittest.TestCase):
    def test_detected_ranges_match_manual_ranges(self):
        checked = 0
        for (corp_code, rcept_no), expected in REAL_REPORTS.items():
            pages = real_pages(corp_code, rcept_no)
            if pages is None:
                continue
            with self.subTest(corp_code=corp_code):
                plan = detect_sections(pages)
                self.assertEqual([r.as_argument() for r in plan.ranges], expected)
                checked += 1
        if not checked:
            self.skipTest("실제 PDF 페이지 JSON이 없습니다.")

    def test_samsung_auto_gold_pages_match_manual_eval_set(self):
        pages = real_pages("00126380", "20260310002820")
        dataset_path = PROJECT_ROOT / "evaluation" / "dataset" / "eval_retrieval.json"
        if pages is None or not dataset_path.is_file():
            self.skipTest("삼성전자 PDF 페이지 JSON 또는 평가셋이 없습니다.")
        manual = {item["id"]: item for item in json.loads(dataset_path.read_text(encoding="utf-8"))}

        def won(item_id: str, year: str | None = None) -> str:
            value = manual[item_id]["expected_value"]
            value = value[year] if year else value
            amount = int(value.strip("()").replace(",", "")) * 1_000_000
            return str(-amount if value.startswith("(") else amount)

        accounts = [
            {"sj_div": "BS", "account_id": "ifrs-full_Assets", "account_nm": "자산총계",
             "thstrm_amount": won("S01"), "frmtrm_amount": won("S09", "2024")},
            {"sj_div": "BS", "account_id": "ifrs-full_CashAndCashEquivalents", "account_nm": "현금및현금성자산",
             "thstrm_amount": won("S02"), "frmtrm_amount": ""},
            {"sj_div": "IS", "account_id": "ifrs-full_Revenue", "account_nm": "매출액",
             "thstrm_amount": won("S04"), "frmtrm_amount": won("S10", "2024")},
            {"sj_div": "CF", "account_id": "ifrs-full_CashFlowsFromUsedInOperatingActivities",
             "account_nm": "영업활동현금흐름", "thstrm_amount": won("S07"), "frmtrm_amount": ""},
            {"sj_div": "CF", "account_id": "ifrs-full_DividendsPaidClassifiedAsFinancingActivities",
             "account_nm": "배당금의 지급", "thstrm_amount": won("S08"), "frmtrm_amount": won("S12", "2024")},
        ]
        page_map = {page["page_number"]: page["text"] for page in pages}
        items, _ = build_auto_eval_set(accounts, page_map, detect_sections(pages), "20260310002820.pdf", 2025)
        auto = {item["question"]: item for item in items}
        for item_id in ["S01", "S02", "S04", "S07", "S08", "S09", "S10"]:
            with self.subTest(item_id=item_id):
                question = manual[item_id]["question"]
                self.assertEqual(auto[question]["gold_sources"], manual[item_id]["gold_sources"])


if __name__ == "__main__":
    unittest.main()
