"""XML v2 신규 기업 선택 및 draft 생성 흐름."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts.pipelines import run_pipeline_xml_v2 as pipeline



class CompanySelectionTest(unittest.TestCase):
    def test_same_name_requires_explicit_selection(self):
        from fin_research.ingestion.auto_resolve import CorpInfo
        corps = [CorpInfo('00000001', '동명', '123456'), CorpInfo('00000002', '동명', '')]
        from unittest.mock import Mock
        client = Mock(api_key='key')
        client._get_json.side_effect = [
            {'ceo_nm': '대표1', 'induty_code': '26111'},
            {'ceo_nm': '대표2', 'induty_code': '64110'},
        ]
        with patch('builtins.input', side_effect=['0', 'abc', '2']):
            selected = pipeline.select_company('동명', corps, client)
        self.assertEqual(selected.corp_code, '00000002')
        self.assertEqual(client._get_json.call_count, 2)
        self.assertEqual(pipeline.select_company('00000001', corps, client), corps[0])
        with self.assertRaisesRegex(ValueError, '기업이 없습니다'):
            pipeline.select_company('없는회사', corps, client)

    def test_missing_report_includes_company_year_and_publication_hint(self):
        import argparse
        from unittest.mock import Mock
        from fin_research.ingestion.auto_resolve import CorpInfo

        args = argparse.Namespace(company='삼성전자', corp_code=None, rcept_no=None,
                                  fiscal_year=2026, refresh_corp_codes=False,
                                  report_choice='latest')
        client = Mock()
        client.search_business_reports.return_value = []
        with patch.object(pipeline.DartClient, 'from_environment', return_value=client), \
             patch.object(pipeline, 'load_corp_codes', return_value=[CorpInfo('00126380', '삼성전자', '005930')]):
            with self.assertRaises(ValueError) as error:
                pipeline.resolve_target_from_company(args, argparse.ArgumentParser())
        self.assertIn('삼성전자', str(error.exception))
        self.assertIn('2026년 사업보고서가 조회되지 않았습니다', str(error.exception))
        self.assertIn('2027년 공시 목록', str(error.exception))
        self.assertIn('아직 공시되지 않았거나', str(error.exception))



if __name__ == '__main__':
    unittest.main()
