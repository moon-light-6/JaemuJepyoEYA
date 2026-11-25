"""6가지 유형의 loop_qa를 모의 실행하거나 저장된 실제 실행 결과를 채점한다."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evaluation.loop_qa import LoopObservation, LoopQACase, load_jsonl
from evaluation.metrics.loop_metrics import evaluate_case, summarize


def evaluate_observations(cases: list[LoopQACase], observations: list[LoopObservation]) -> dict:
    """실행 결과와 정답을 ID로 연결하고 누락은 실패, 알 수 없는 ID는 입력 오류로 처리한다."""
    case_ids = {case.id for case in cases}
    if len(case_ids) != len(cases):
        raise ValueError("평가 질문 ID가 중복되었습니다.")
    by_id = {observation.case_id: observation for observation in observations}
    if len(by_id) != len(observations):
        raise ValueError("실행 결과 ID가 중복되었습니다.")
    if set(by_id) - case_ids:
        raise ValueError(f"평가셋에 없는 실행 결과 ID입니다: {sorted(set(by_id) - case_ids)}")
    results = [evaluate_case(case, by_id.get(case.id)) for case in cases]
    return {"summary": summarize(results), "cases": [result.model_dump() for result in results]}


def run_mock(cases: list[LoopQACase], *, integrated: bool = False) -> list[LoopObservation]:
    """정답과 분리된 시나리오로 기존 구성요소를 실행하며 예외도 실패 결과로 남긴다."""
    from evaluation.loop_fixture_runner import run_fixture_case
    from evaluation.integrated_fixture_runner import run_integrated_fixture

    observations = []
    for case in cases:
        print(f"[{case.id}] {case.category}: {case.question}", file=sys.stderr, flush=True)
        try:
            observations.append(run_integrated_fixture(case) if integrated else run_fixture_case(case))
        except Exception as exc:
            observations.append(LoopObservation(
                case_id=case.id, status="error", answer="", stop_reason="fixture_error",
                limitations=[f"{type(exc).__name__}: {exc}"],
            ))
    return observations


def main() -> int:
    """평가 모드·질문·결과 경로를 받아 JSON 보고서와 재사용 가능한 실행 관측값을 저장한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evaluation/dataset/loop_qa.jsonl")
    parser.add_argument("--mode", choices=("mock", "integration-mock", "predictions"), default="mock")
    parser.add_argument("--predictions", type=Path, help="LoopObservation 형식의 실행 결과 JSONL")
    parser.add_argument("--out", type=Path, default=ROOT / "data/runs/loop_qa_report.json")
    parser.add_argument("--observations-out", type=Path, help="모의 실행 결과 JSONL 저장 경로")
    args = parser.parse_args()
    if args.mode == "predictions" and not args.predictions:
        parser.error("predictions 모드에는 --predictions가 필요합니다.")
    if args.mode != "predictions" and args.predictions:
        parser.error("모의 실행에서는 --predictions를 사용하지 않습니다.")
    try:
        cases = load_jsonl(args.dataset, LoopQACase)
        observations = (run_mock(cases, integrated=args.mode == "integration-mock")
                        if args.mode != "predictions" else load_jsonl(args.predictions, LoopObservation))
        report = evaluate_observations(cases, observations)
        report.update({
            "mode": args.mode, "dataset": str(args.dataset.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data_origins": dict(Counter(case.data_origin for case in cases)),
            "assessment_scope": (
                "production_tool_graph_with_fixture_io" if args.mode == "integration-mock" else
                "component_fixture_and_document_loop_control" if args.mode == "mock" else "submitted_observations"
            ),
            "limitations": [
                "모의 실행의 도구 선택·LLM 판단은 시나리오로 고정되어 실제 선택 성능을 뜻하지 않습니다.",
                "문구 포함 여부는 의미·인과관계 전체의 검증이 아닙니다.",
                "제출 결과 채점은 관측값 기준이며 실제 도구 실행 여부나 출처 원본을 자동 인증하지 않습니다.",
            ],
        })
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.observations_out:
            args.observations_out.parent.mkdir(parents=True, exist_ok=True)
            args.observations_out.write_text("".join(
                observation.model_dump_json() + "\n" for observation in observations
            ), encoding="utf-8")
        summary = report["summary"]
        print(f"평가 모드: {args.mode} · {summary['passed']}/{summary['total']} 통과")
        for row in summary["categories"].values():
            print(f"  {row['label']}: {row['passed']}/{row['total']}")
        for result in report["cases"]:
            if not result["passed"]:
                print(f"  실패 {result['case_id']}: {', '.join(result['failure_types'])}")
        print(f"보고서: {args.out.resolve()}")
        if args.mode != "predictions":
            print("모의 구성요소·반복 제어 평가이며 전체 시스템 또는 실제 LLM의 정확도가 아닙니다.")
        return 0 if summary["passed"] == summary["total"] else 1
    except (OSError, ValueError) as exc:
        parser.exit(2, f"평가 입력 오류: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
