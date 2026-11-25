
"""XML draft 평가 전용 검색 파이프라인.

run_pipeline_xml_v2.py가 생성한 XML v2 청크와
draft 평가 JSON을 입력받아 검색 평가까지 수행한다.

기존 run_pipeline_xml.py 및 run_pipeline_xml_v2.py는 수정하지 않는다.

주의:
    이 파이프라인의 평가 지표는 verified gold가 아닌
    candidate_sources를 기준으로 계산한 참고용 지표다.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


# 프로젝트 루트 경로
PROJECT_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from fin_research.retrieval.chroma_retriever import EMBEDDING_MODEL
from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.xml_draft_evaluation import (
    load_xml_draft_eval_dataset,
)
from fin_research.retrieval.xml_retrieval import (
    build_xml_chroma,
    load_xml_chunk_documents,
)

# 기존 검색 평가 로직 재사용
from scripts.pipelines.run_pipeline_xml import evaluate_xml


def write_json(path: Path, data: Any) -> None:
    """JSON 결과를 저장한다."""

    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def calculate_sha256(path: Path) -> str:
    """파일의 SHA-256 해시를 계산한다."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_chunk_target(
    documents: list,
    target: SearchTarget,
) -> None:
    """청크 데이터가 요청한 검색 대상과 일치하는지 검사한다."""

    companies = set()

    for document in documents:
        metadata = document.metadata

        if int(metadata["fiscal_year"]) != target.fiscal_year:
            raise ValueError(
                f"사업연도 불일치: {metadata['chunk_id']}"
            )

        if metadata["statement_scope"] != target.statement_scope:
            raise ValueError(
                f"연결/별도 기준 불일치: {metadata['chunk_id']}"
            )

        companies.add(metadata["company"])

    if len(companies) != 1:
        raise ValueError(
            f"청크에 여러 기업이 포함되어 있습니다: {sorted(companies)}"
        )


def validate_candidate_sources(
    questions: list[dict[str, Any]],
    documents: list,
) -> None:
    """Draft 후보 근거가 실제 검색 청크에 존재하는지 확인한다."""

    available_sources = {
        (
            document.metadata["source_file"],
            document.metadata["source_xpath"],
        )
        for document in documents
    }

    missing_questions = []

    for question in questions:
        candidates = question["candidate_sources"]

        matched = any(
            (
                source["source_file"],
                source["source_xpath"],
            ) in available_sources
            for source in candidates
        )

        if not matched:
            missing_questions.append(question["id"])

    if missing_questions:
        raise ValueError(
            "검색 청크에서 후보 근거를 찾을 수 없는 질문이 있습니다: "
            f"{missing_questions}"
        )


def convert_draft_report(
    report: dict[str, Any],
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """기존 평가 결과를 draft 평가 결과 형식으로 변환한다.

    기존 evaluate_xml()은 gold_sources라는 필드명을 사용한다.
    저장 결과에서는 이를 candidate_sources로 바꿔 오해를 방지한다.
    """

    question_map = {
        question["id"]: question
        for question in questions
    }

    for method, rows in report["details"].items():
        for row in rows:
            question = question_map[row["id"]]

            # 검증된 Gold가 아닌 자동 생성 후보 근거임을 명시
            row["candidate_sources"] = row.pop("gold_sources")
            row["evaluation_mode"] = "draft"
            row["reference_type"] = "candidate_sources"

            row["expected_value"] = question.get("expected_value")
            row["unit"] = question.get("unit")

    report["evaluation_mode"] = "draft"
    report["reference_type"] = "candidate_sources"
    report["official_evaluation"] = False

    report["warning"] = (
        "본 결과는 자동 생성된 candidate_sources를 기준으로 계산한 "
        "참고용 검색 평가 결과입니다. 검증된 정답 기반의 공식 성능 지표가 아닙니다."
    )

    return report


def run_draft_evaluation(
    target: SearchTarget,
    chunks_path: Path,
    dataset_path: Path,
    output_path: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    """청크 및 draft 평가셋을 이용해 검색 평가를 실행한다."""

    # 1. 입력 파일 확인
    if not chunks_path.is_file():
        raise FileNotFoundError(
            f"XML v2 청크 파일이 없습니다: {chunks_path}"
        )

    if not dataset_path.is_file():
        raise FileNotFoundError(
            f"Draft 평가 JSON 파일이 없습니다: {dataset_path}"
        )

    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"기존 평가 결과를 보호하기 위해 저장을 중단합니다: {output_path}\n"
            "--overwrite 옵션으로 다시 실행할 수 있습니다."
        )

    # 2. Draft 질문 로딩
    print("\n[1/6] Draft 평가 질문 로딩", flush=True)

    questions = load_xml_draft_eval_dataset(dataset_path)

    print(
        f"평가 질문: {len(questions)}개",
        flush=True,
    )

    # 3. XML 청크 로딩 및 검증
    print("\n[2/6] XML v2 청크 로딩", flush=True)

    documents = load_xml_chunk_documents(chunks_path)

    validate_chunk_target(documents, target)

    validate_candidate_sources(questions, documents)

    print(
        f"검색 대상 청크: {len(documents)}개",
        flush=True,
    )

    # 4. 파일 해시 계산
    chunks_hash = calculate_sha256(chunks_path)
    dataset_hash = calculate_sha256(dataset_path)

    company = documents[0].metadata["company"]

    # 5. 임시 Chroma 인덱스 생성 및 검색 평가
    print("\n[3/6] 임시 Chroma 인덱스 생성", flush=True)

    with TemporaryDirectory(
        prefix="xml_draft_eval_"
    ) as temp_dir:
        
        chroma_dir = Path(temp_dir) / "chroma"

        try:
            count = build_xml_chroma(
                chunks_path=chunks_path,
                persist_directory=chroma_dir,
                collection_name=target.xml_collection_name,
            )

            if count != len(documents):
                raise ValueError(
                    f"Chroma 색인 수 불일치: "
                    f"입력={len(documents)}, 색인={count}"
                )

            print(
                f"Chroma 색인 완료: {count}개",
                flush=True,
            )

            # 6. 기존 검색 평가 함수 재사용
            print("\n[4/6] 검색기 평가 실행", flush=True)

            report = evaluate_xml(
                questions=questions,
                chunks_path=chunks_path,
                chroma_dir=chroma_dir,
                target=target,
            )

            print("\n[5/6] 평가 결과 변환", flush=True)

            report = convert_draft_report(
                report,
                questions,
            )

            report["config"].update({
                "evaluation_mode": "draft",
                "reference_type": "candidate_sources",
                "official_evaluation": False,
                "company": company,
                "corp_code": target.corp_code,
                "fiscal_year": target.fiscal_year,
                "statement_scope": target.statement_scope,
                "rcept_no": target.rcept_no,
                "questions": len(questions),
                "chunks": len(documents),
                "chunks_path": str(chunks_path),
                "dataset_path": str(dataset_path),
                "chunks_sha256": chunks_hash,
                "dataset_sha256": dataset_hash,
                "embedding_model": EMBEDDING_MODEL,
                "chroma_temporary": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

            # 임시 인덱스와 결과 파일은 별도 관리한다.
            # 결과는 보존하고 Chroma만 삭제한다.
            print("\n[6/6] 평가 결과 저장", flush=True)

            write_json(output_path, report)

        # Chroma 내부 DB 리소스를 종료한 뒤 임시 폴더를 정리한다.
        finally:
            close_chroma_client(chroma_dir)
            gc.collect()

    print(f"\n평가 결과 저장 완료: {output_path}")
    print("임시 Chroma 인덱스 정리 완료")

    return report


def print_summary(report: dict[str, Any]) -> None:
    """검색 평가 결과를 표 형태로 출력한다."""

    metrics = [
        ("위치+값@3", "source_value_hit_at_3"),
        ("위치+값@5", "source_value_hit_at_5"),
        ("위치@3", "hit_at_3"),
        ("위치@5", "hit_at_5"),
        ("값@3", "value_hit_at_3"),
        ("값@5", "value_hit_at_5"),
    ]

    methods = [
        ("BM25", "bm25"),
        ("Chroma", "chroma"),
        ("MMR", "mmr"),
        ("Hybrid-RRF", "hybrid"),
    ]

    print("\n========== Draft 검색 평가 결과 ==========")
    print("평가 질문 수:", report["config"]["questions"])
    print("검색 청크 수:", report["config"]["chunks"])

    print("\n방식\t" + "\t".join(name for name, _ in metrics))

    for name, key in methods:
        row = report["summary"][key]

        values = [
            "N/A" if row[metric] is None
            else f"{row[metric] * 100:.1f}%"
            for _, metric in metrics
        ]

        print(name + "\t" + "\t".join(values))

    print("\n※ candidate_sources 기반의 참고용 평가 결과입니다.")


def parse_args() -> argparse.Namespace:
    """CLI 인자를 정의한다."""

    parser = argparse.ArgumentParser(
        description="XML draft 검색 평가 파이프라인"
    )

    parser.add_argument(
        "--corp-code",
        required=True,
        help="OpenDART 고유번호 8자리",
    )

    parser.add_argument(
        "--fiscal-year",
        required=True,
        type=int,
        help="사업연도",
    )

    parser.add_argument(
        "--statement-scope",
        required=True,
        choices=("consolidated", "separate"),
        help="연결 또는 별도",
    )

    parser.add_argument(
        "--rcept-no",
        required=True,
        help="DART 공시 접수번호 14자리",
    )

    parser.add_argument(
        "--chunks-path",
        type=Path,
        help="XML v2 청크 JSON 경로",
    )

    parser.add_argument(
        "--dataset-path",
        type=Path,
        help="Draft 평가 JSON 경로",
    )

    parser.add_argument(
        "--output-path",
        type=Path,
        help="평가 결과 JSON 저장 경로",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="기존 평가 결과 파일 덮어쓰기 허용",
    )

    return parser.parse_args()


def close_chroma_client(persist_directory: Path) -> None:
    """Chroma 클라이언트의 내부 시스템을 종료한다."""

    import chromadb

    if not persist_directory.exists():
        return

    client = chromadb.PersistentClient(
        path=str(persist_directory)
    )

    try:
        # Chroma 내부 DB 및 리소스 종료
        client._system.stop()
    finally:
        del client
        gc.collect()


def main() -> int:
    """Draft 검색 평가 파이프라인 진입점."""

    args = parse_args()

    target = SearchTarget(
        corp_code=args.corp_code,
        fiscal_year=args.fiscal_year,
        statement_scope=args.statement_scope,
        rcept_no=args.rcept_no,
    )

    # v2 파이프라인 결과 경로
    chunks_path = args.chunks_path or (
        target.xml_scope_dir(PROJECT_ROOT)
        / f"{target.rcept_no}_xml_chunks_v2.json"
    )

    # draft 평가 JSON 경로
    dataset_path = (
        args.dataset_path
        or target.xml_dataset_path(PROJECT_ROOT)
    )

    # 기존 검색 평가 결과와 구분되는 별도 파일
    output_path = args.output_path or (
        target.xml_result_path(PROJECT_ROOT).with_name(
            f"{target.rcept_no}_retrieval_draft_eval.json"
        )
    )

    report = run_draft_evaluation(
        target=target,
        chunks_path=chunks_path,
        dataset_path=dataset_path,
        output_path=output_path,
        overwrite=args.overwrite,
    )

    print_summary(report)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"\nDraft 검색 평가 실패: {error}", file=sys.stderr)
        raise SystemExit(1) from error
