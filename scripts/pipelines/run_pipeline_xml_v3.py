"""v3 문서·청크 준비와 선택적인 재무 질문 초안 생성 및 검색 평가."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'src'))

from fin_research.ingestion.dart_client import DartClient
from fin_research.ingestion.auto_resolve import fetch_financial_accounts
from fin_research.ingestion.xml_documents_v3 import build_xml_documents_v3
from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.xml_retrieval import build_xml_chroma, load_xml_eval_dataset
from evaluation.xml_draft_dataset import build_xml_draft_dataset, detect_statement_unit
from scripts.pipelines.run_pipeline_xml import evaluate_xml, load_questions, write_json
from scripts.pipelines.run_pipeline_xml_v2 import resolve_target_from_company
from scripts.processing.build_xml_chunks_v3 import build_xml_chunks


def main():
    parser = argparse.ArgumentParser(description='사업보고서와 감사보고서 v3 준비 및 선택적 검색 평가')
    parser.add_argument('--company', help='기업명 또는 코드')
    parser.add_argument('--corp-code', help='OpenDART 고유번호 8자리')
    parser.add_argument('--rcept-no', help='접수번호 14자리')
    parser.add_argument('--fiscal-year', required=True, type=int)
    parser.add_argument('--statement-scope', required=True, choices=('consolidated', 'separate'))
    parser.add_argument('--report-choice', choices=('latest', 'original'), default='latest')
    parser.add_argument('--refresh-corp-codes', action='store_true')
    parser.add_argument('--zip-path', type=Path, help='기존 ZIP 사용')
    parser.add_argument('--output-dir', type=Path, help='v3 결과 저장 폴더')
    parser.add_argument('--chunk-size', type=int, default=600)
    parser.add_argument('--chunk-overlap', type=int, default=100)
    parser.add_argument('--overwrite', action='store_true', help='기존 v3 결과 덮어쓰기')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--make-draft-dataset', action='store_true', help='기존 v2 방식의 재무 질문 초안 생성')
    mode.add_argument('--prepare-only', action='store_true', help='문서와 청크만 생성하고 종료')
    parser.add_argument('--dataset-path', type=Path, help='평가에 사용할 검증된 평가셋 (기본: 기존 v1+v2)')
    parser.add_argument('--chroma-dir', type=Path, help='v3 평가 전용 Chroma 상위 폴더')
    if len(sys.argv) == 1:
        parser.print_help()
        return
    args = parser.parse_args()
    evaluate = not (args.make_draft_dataset or args.prepare_only)
    if args.dataset_path and not evaluate:
        parser.error('--dataset-path는 평가 실행에서만 사용할 수 있습니다.')
    if args.chroma_dir and not evaluate:
        parser.error('--chroma-dir는 평가 실행에서만 사용할 수 있습니다.')
    if args.chunk_size <= 0 or not 0 <= args.chunk_overlap < args.chunk_size:
        parser.error('chunk-size는 양수이고 chunk-overlap은 0 이상 chunk-size 미만이어야 합니다.')
    # 세 값이 명시되면 오프라인에서도 기존 ZIP을 처리할 수 있다.
    if not (args.company and args.corp_code and args.rcept_no):
        resolve_target_from_company(args, parser)
    target = SearchTarget(args.corp_code, args.fiscal_year, args.statement_scope, args.rcept_no)
    if not args.company:
        parser.error('기업명을 --company로 지정하세요.')
    directory = args.output_dir or target.xml_scope_dir(PROJECT_ROOT)
    paths = {name: directory / f'{target.rcept_no}_xml_{name}_v3.json' for name in ('documents', 'chunks', 'pipeline')}
    existing = [str(path) for path in paths.values() if path.exists()]
    reuse = bool(existing) and len(existing) == len(paths) and not args.overwrite and (args.make_draft_dataset or evaluate)
    if existing and not args.overwrite and not reuse:
        raise FileExistsError('기존 v3 결과가 있습니다. 다른 --output-dir 또는 --overwrite를 사용하세요: ' + ', '.join(existing))
    dataset_path = target.xml_dataset_path(PROJECT_ROOT)
    draft_path = dataset_path.with_name(f'{target.rcept_no}_retrieval_v3_draft.json')
    result_path = target.xml_result_path(PROJECT_ROOT).with_name(f'{target.rcept_no}_retrieval_v3.json')
    if args.make_draft_dataset and draft_path.exists() and not args.overwrite:
        raise FileExistsError(f'기존 v3 초안이 있습니다: {draft_path}')
    if evaluate and result_path.exists() and not args.overwrite:
        raise FileExistsError(f'기존 v3 평가 결과가 있습니다: {result_path}')
    questions = None
    if evaluate:
        if args.dataset_path:
            questions = load_xml_eval_dataset(args.dataset_path)
        else:
            extra = target.xml_dataset_v2_path(PROJECT_ROOT)
            questions = load_questions(dataset_path, extra if extra.exists() else None)
    if reuse:
        print(f'기존 v3 문서와 청크 사용: {directory}')
        documents = json.loads(paths['documents'].read_text(encoding='utf-8'))
        metadata = json.loads(paths['pipeline'].read_text(encoding='utf-8'))
        chunks = None
    else:
        documents, chunks, metadata = prepare(args, target, paths, directory)
    if args.make_draft_dataset:
        # 재무제표 원본으로 선택한 XML의 문서만 후보로 사용한다.
        source = metadata['audit']['source_xml']
        eligible = [d for d in documents if d.get('source_file') == source
                    and d.get('document_type') in ('xml_financial_statement', 'xml_note')
                    and d.get('content_scope') == target.statement_scope]
        if not eligible:
            raise ValueError('요청 기준의 재무제표·주석 문서가 없습니다.')
        accounts = fetch_financial_accounts(DartClient.from_environment(), target.corp_code,
                                            target.fiscal_year, target.statement_scope)
        receipts = {row.get('rcept_no') for row in accounts}
        if accounts and receipts != {target.rcept_no}:
            raise ValueError(f'재무 계정 접수번호가 요청과 다릅니다: {receipts}')
        unit = detect_statement_unit(eligible)
        items, skipped = build_xml_draft_dataset(accounts, eligible, target.fiscal_year, target.statement_scope, unit=unit)
        write_json(draft_path, items)
        print(f'질문 초안: {draft_path} ({len(items)}문항, 단위 {unit})')
        for reason in skipped:
            print(f'  제외: {reason}')
        print('원문과 정답 위치를 검토한 후 annotation_status를 verified로 변경하세요.')
    elif evaluate:
        digest = hashlib.sha256(paths['chunks'].read_bytes()).hexdigest()
        chroma_dir = (args.chroma_dir or PROJECT_ROOT / 'data' / 'chroma_xml_v3') / digest
        count = build_xml_chroma(paths['chunks'], chroma_dir, target.xml_collection_name)
        report = evaluate_xml(questions, paths['chunks'], chroma_dir, target)
        report['config'].update({'chunks_path': str(paths['chunks']), 'chunks_sha256': digest,
                                 'dataset_path': str(args.dataset_path or dataset_path),
                                 'additional_dataset_path': str(target.xml_dataset_v2_path(PROJECT_ROOT))
                                 if not args.dataset_path and target.xml_dataset_v2_path(PROJECT_ROOT).exists() else None,
                                 'chroma_dir': str(chroma_dir)})
        write_json(result_path, report)
        print(f'색인 청크: {count}개 / 검증 문항: {len(questions)}개')
        for name, row in report['summary'].items():
            score = row['source_value_hit_at_5']
            print(f'{name} 위치+값@5: {score * 100:.1f}%' if score is not None else f'{name} 위치+값@5: N/A')
        print(f'평가 결과: {result_path}')


def prepare(args, target, paths, directory):
    zip_path = args.zip_path or PROJECT_ROOT / 'data' / 'raw' / 'dart' / target.corp_code / str(target.fiscal_year) / f'{target.rcept_no}.zip'
    if not zip_path.is_file():
        if args.zip_path:
            raise FileNotFoundError(f'ZIP을 찾지 못했습니다: {zip_path}')
        print(f'OpenDART ZIP 다운로드: {target.rcept_no}', flush=True)
        DartClient.from_environment().download_original_zip(target.rcept_no, zip_path)
    documents, metadata = build_xml_documents_v3(zip_path, target.corp_code, target.fiscal_year, target.statement_scope, args.company)
    chunks = build_xml_chunks(documents, args.chunk_size, args.chunk_overlap)
    metadata.update({'zip_path': str(zip_path), 'corp_code': target.corp_code, 'fiscal_year': target.fiscal_year,
                     'statement_scope': target.statement_scope, 'rcept_no': target.rcept_no,
                     'chunk_size': args.chunk_size, 'chunk_overlap': args.chunk_overlap,
                     'documents': len(documents), 'chunks': len(chunks)})
    directory.mkdir(parents=True, exist_ok=True)
    for key, value in (('documents', documents), ('chunks', chunks), ('pipeline', metadata)):
        paths[key].write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"본문 XML: {metadata['business_report_xml']} ({metadata['business_documents']}개 문서)")
    print(f"재무제표 원본: {metadata['audit']['source_xml']} ({metadata['audit_documents']}개 문서)")
    print(f'청크: {len(chunks)}개')
    for key, path in paths.items():
        print(f'{key}: {path}')
    return documents, chunks, metadata


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'실패: {exc}', file=sys.stderr)
        sys.exit(1)
