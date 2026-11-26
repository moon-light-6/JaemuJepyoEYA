import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))
from scripts.pipelines import run_pipeline_xml_v3 as pipeline


class V3ModesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / 'out'
        self.out.mkdir()
        self.receipt = '20260323001497'
        self.args = ['pipeline', '--company', '테스트', '--corp-code', '01136977',
                     '--rcept-no', self.receipt, '--fiscal-year', '2025',
                     '--statement-scope', 'consolidated', '--output-dir', str(self.out)]
        docs = [dict(source_file='audit.xml', document_type='xml_financial_statement', content_scope='consolidated', text='단위: 원'),
                dict(source_file='body.xml', document_type='xml_business_report', content_scope='separate', text='wrong')]
        for name, obj in [('documents', docs), ('chunks', [{'chunk_id': 'c1'}]),
                          ('pipeline', {'audit': {'source_xml': 'audit.xml'}})]:
            (self.out / f'{self.receipt}_xml_{name}_v3.json').write_text(json.dumps(obj), encoding='utf-8')

    def test_draft_reuses_documents_and_limits_candidates(self):
        with patch.object(pipeline, 'PROJECT_ROOT', self.root), patch.object(sys, 'argv', self.args + ['--make-draft-dataset']), \
             patch.object(pipeline.DartClient, 'from_environment', return_value=object()), \
             patch.object(pipeline, 'fetch_financial_accounts', return_value=[{'rcept_no': self.receipt}]), \
             patch.object(pipeline, 'build_xml_draft_dataset', return_value=([{'annotation_status': 'draft'}], [])) as build:
            pipeline.main()
        self.assertEqual([d['source_file'] for d in build.call_args.args[1]], ['audit.xml'])
        draft = self.root / 'evaluation/dataset/xml/01136977/2025/consolidated/20260323001497_retrieval_v3_draft.json'
        self.assertEqual(json.loads(draft.read_text())[0]['annotation_status'], 'draft')

    def test_prepare_only_does_not_load_questions_or_build_index(self):
        fresh = self.root / 'fresh'
        args = self.args[:-1] + [str(fresh), '--prepare-only']
        with patch.object(pipeline, 'PROJECT_ROOT', self.root), patch.object(sys, 'argv', args), \
             patch.object(pipeline, 'prepare', return_value=([], [], {})) as prepare, \
             patch.object(pipeline, 'load_questions') as questions, \
             patch.object(pipeline, 'build_xml_chroma') as index:
            pipeline.main()
        prepare.assert_called_once()
        questions.assert_not_called()
        index.assert_not_called()

    def test_evaluate_uses_verified_dataset_and_separate_index(self):
        dataset = self.root / 'verified.json'
        dataset.write_text(json.dumps([{'id': 'X1', 'question': 'test', 'annotation_status': 'verified',
                                        'gold_sources': [{'source_file': 'audit.xml', 'source_xpath': '/D[1]'}]}]))
        report = {'config': {}, 'summary': {'bm25': {'source_value_hit_at_5': 1.0}}}
        with patch.object(pipeline, 'PROJECT_ROOT', self.root), \
             patch.object(sys, 'argv', self.args + ['--dataset-path', str(dataset)]), \
             patch.object(pipeline, 'build_xml_chroma', return_value=1) as build, \
             patch.object(pipeline, 'evaluate_xml', return_value=report) as evaluate:
            pipeline.main()
        self.assertEqual(len(evaluate.call_args.args[0]), 1)
        self.assertIn('chroma_xml_v3', str(build.call_args.args[1]))
        self.assertTrue((self.root / 'evaluation/results/xml/01136977/2025/consolidated/20260323001497_retrieval_v3.json').exists())

    def test_draft_dataset_cannot_be_evaluated(self):
        dataset = self.root / 'draft.json'
        dataset.write_text(json.dumps([{'annotation_status': 'draft'}]))
        with patch.object(pipeline, 'PROJECT_ROOT', self.root), \
             patch.object(sys, 'argv', self.args + ['--dataset-path', str(dataset)]), \
             self.assertRaises(ValueError):
            pipeline.main()
