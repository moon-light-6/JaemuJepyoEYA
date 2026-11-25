"""DART XML v2 준비와 Draft 검색 평가를 연결하는 통합 실행기.

기존 run_pipeline_xml_v2.py / run_pipeline_xml_draft_eval.py는 수정하지 않는다.
실패 단계와 원인은 터미널에만 출력하며 별도 로그 파일은 만들지 않는다.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
V2_SCRIPT = ROOT / "scripts" / "pipelines" / "run_pipeline_xml_v2.py"
EVAL_SCRIPT = ROOT / "scripts" / "pipelines" / "run_pipeline_xml_draft_eval.py"
METHODS = {"bm25", "chroma", "mmr", "hybrid"}


@dataclass
class CommandResult:
    returncode: int
    last_stage: str
    error_message: str
    cleanup_winerror32: bool = False


class PipelineFailed(RuntimeError):
    def __init__(self, stage: str, message: str, returncode: int | None = None):
        super().__init__(message)
        self.stage = stage
        self.returncode = returncode


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_report(report_path: Path, chunks_path: Path, dataset_path: Path) -> bool:
    """해당 입력으로 생성된 draft 검색 평가 결과인지 검증한다."""
    if not (report_path.is_file() and chunks_path.is_file() and dataset_path.is_file()):
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
        config = report["config"]
        details = report["details"]
        return (
            report.get("evaluation_mode") == "draft"
            and report.get("official_evaluation") is False
            and config.get("chunks_sha256") == sha256(chunks_path)
            and config.get("dataset_sha256") == sha256(dataset_path)
            and set(report["summary"]) == METHODS
            and set(details) == METHODS
            and isinstance(dataset, list)
            and bool(dataset)
            and config.get("questions") == len(details["bm25"])
            and all(len(details[method]) == config["questions"] for method in METHODS)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def identify_stage(line: str, pipeline: str, previous: str) -> str:
    """하위 스크립트의 기존 출력 문구로 마지막 확인 단계 파악."""
    if pipeline == "prepare":
        stages = (
            ("OpenDART 식별자:", "기업·사업보고서 식별 완료"),
            ("OpenDART XML 원본 ZIP 수집:", "DART XML 원본 다운로드"),
            ("원본 ZIP에서 XML 문서 생성:", "XML 문서 생성"),
            ("XML 문서 저장:", "XML 문서 저장"),
            ("v2 청크 생성:", "XML v2 청킹"),
            ("draft XML 평가셋:", "Draft 평가셋 생성"),
        )
    else:
        stages = (
            ("[1/6]", "Draft 평가 질문 로딩"),
            ("[2/6]", "XML v2 청크 로딩·검증"),
            ("[3/6]", "Chroma 색인"),
            ("[4/6]", "검색기 4종 평가"),
            ("[5/6]", "평가 결과 변환"),
            ("[6/6]", "평가 결과 저장·임시 Chroma 정리"),
        )
    for marker, stage in stages:
        if marker in line:
            return stage
    return previous


def extract_error(lines: list[str], returncode: int) -> str:
    """최종 출력에서 오류 메시지를 뽑되, 진척률 표시를 원인으로 오인하지 않는다."""
    for line in reversed(lines):
        cleaned = line.strip()
        for marker in ("Draft 검색 평가 실패:", "실패:", "Error:", "ERROR:"):
            if marker in cleaned:
                return cleaned.split(marker, 1)[1].strip() or cleaned
    for line in reversed(lines):
        cleaned = line.strip()
        if not cleaned or "Loading weights:" in cleaned or cleaned.startswith("Warning:"):
            continue
        if "Traceback" in cleaned or cleaned.startswith("File ") or cleaned.startswith("$"):
            continue
        return cleaned[:600]
    return f"하위 프로세스가 종료 코드 {returncode}로 끝났습니다. 상세 오류 메시지는 없습니다."


def run_command(command: list[str], *, pipeline: str, env: dict[str, str] | None = None) -> CommandResult:
    """하위 출력을 실시간 표시하고, 마지막 단계와 오류 원인을 메모리에만 유지."""
    print("\n$ " + subprocess.list2cmdline(command), flush=True)
    last_stage = "기업·사업보고서 조회" if pipeline == "prepare" else "Draft 평가 시작"
    tail: deque[str] = deque(maxlen=50)
    cleanup_winerror32 = False

    child_env = os.environ.copy()
    if env is not None:
        child_env.update(env)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"

    process = subprocess.Popen(
        command, cwd=ROOT, env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            tail.append(line.rstrip("\r\n"))
            last_stage = identify_stage(line, pipeline, last_stage)
            if "[WinError 32]" in line and "xml_draft_eval_" in line:
                cleanup_winerror32 = True
        returncode = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise PipelineFailed(last_stage, "사용자가 Ctrl+C로 실행을 중단했습니다.") from None
    finally:
        if process.stdout is not None:
            process.stdout.close()

    return CommandResult(
        returncode=returncode,
        last_stage=last_stage,
        error_message=extract_error(list(tail), returncode) if returncode else "",
        cleanup_winerror32=cleanup_winerror32,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="DART XML 준비부터 Draft 검색 평가까지 한 번에 실행")
    parser.add_argument("--corp-code", required=True, help="OpenDART 고유번호 8자리")
    parser.add_argument("--fiscal-year", type=int, required=True)
    parser.add_argument("--statement-scope", choices=("consolidated", "separate"), required=True)
    parser.add_argument("--rcept-no", required=True, help="DART 공시 접수번호 14자리")
    parser.add_argument("--overwrite", action="store_true", help="기존 평가 결과를 재생성")
    parser.add_argument("--external", action="store_true", help="외부 기업 평가셋/결과를 evaluation/external/에 저장")
    args = parser.parse_args()

    if not (args.corp_code.isdigit() and len(args.corp_code) == 8):
        parser.error("--corp-code는 8자리 숫자여야 합니다.")
    if not (args.rcept_no.isdigit() and len(args.rcept_no) == 14):
        parser.error("--rcept-no는 14자리 숫자여야 합니다.")

    base = [args.corp_code, str(args.fiscal_year), args.statement_scope]
    chunks_path = ROOT / "data" / "processed" / "xml" / "dart" / Path(*base) / f"{args.rcept_no}_xml_chunks_v2.json"
    dataset_path = ROOT / "evaluation" / "dataset" / "xml" / Path(*base) / f"{args.rcept_no}_retrieval.json"
    report_path = ROOT / "evaluation" / "results" / "xml" / Path(*base) / f"{args.rcept_no}_retrieval_draft_eval.json"

    # 기존 팀원 파이프라인의 기본 출력 위치는 변경하지 않는다.
    # 외부 기업에 한해서만 통합 실행기가 평가셋과 결과 위치를 분리한다.
    default_dataset_path = dataset_path
    if args.external:
        external_root = ROOT / "evaluation" / "external"
        dataset_path = external_root / "dataset" / "xml" / Path(*base) / f"{args.rcept_no}_retrieval.json"
        report_path = external_root / "results" / "xml" / Path(*base) / f"{args.rcept_no}_retrieval_draft_eval.json"

    args_common = [
        "--corp-code", args.corp_code,
        "--fiscal-year", str(args.fiscal_year),
        "--statement-scope", args.statement_scope,
        "--rcept-no", args.rcept_no,
    ]

    print(f"\n대상: corp_code={args.corp_code} / 연도={args.fiscal_year} / "
          f"기준={args.statement_scope} / 접수번호={args.rcept_no}", flush=True)
    print(f"데이터 모드: {'외부 기업 (Git 추적 제외)' if args.external else '기본 경로'}", flush=True)

    if not V2_SCRIPT.is_file() or not EVAL_SCRIPT.is_file():
        raise PipelineFailed("시작 전 확인", "통합할 기존 파이프라인 스크립트가 없습니다.")

    print("\n[통합 1/2] XML 문서·청크·Draft 평가셋 준비", flush=True)
    default_dataset_existed = default_dataset_path.is_file()
    if args.external and not dataset_path.is_file() and default_dataset_existed and chunks_path.is_file():
        # 이전에 기본 위치로 생성한 외부 기업 데이터는 기존 파일을 훼손하지 않고 복사한다.
        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(default_dataset_path, dataset_path)
        print(f"기존 Draft를 외부 평가셋 폴더로 복사했습니다: {dataset_path}", flush=True)
        print(f"참고: 기본 경로의 기존 파일은 별도로 확인해 주세요: {default_dataset_path}", flush=True)
    if chunks_path.is_file() and dataset_path.is_file():
        print("준비된 XML v2 청크와 Draft 평가셋을 재사용합니다.", flush=True)
    else:
        result = run_command([
            sys.executable, str(V2_SCRIPT), *args_common, "--make-draft-dataset"
        ], pipeline="prepare")
        if result.returncode != 0:
            raise PipelineFailed(result.last_stage, result.error_message, result.returncode)
        if not chunks_path.is_file():
            raise PipelineFailed("XML v2 청킹", f"준비 단계가 끝났지만 청크 파일이 없습니다: {chunks_path}")
        if args.external:
            # v2 생성기는 --make-draft-dataset 실행 중 --dataset-path를 지원하지 않으므로
            # 기본 위치에 생성된 Draft를 통합 실행기가 외부 위치로 이동/복사한다.
            if not default_dataset_path.is_file():
                raise PipelineFailed("Draft 평가셋 생성", f"준비 단계가 끝났지만 Draft JSON이 없습니다: {default_dataset_path}")
            dataset_path.parent.mkdir(parents=True, exist_ok=True)
            if default_dataset_existed:
                # 실행 전에 이미 있던 파일은 선정 30개 기업의 자산일 수 있으므로 보존.
                shutil.copy2(default_dataset_path, dataset_path)
                print("기존 기본 Draft 파일은 보호하고 외부 경로에 복사했습니다.", flush=True)
                print(f"참고: 기본 경로의 기존 파일은 Git 추적 대상일 수 있습니다: {default_dataset_path}", flush=True)
            else:
                shutil.move(str(default_dataset_path), str(dataset_path))
                print("새로 생성된 Draft 평가셋을 외부 폴더로 이동했습니다.", flush=True)
            print(f"외부 평가셋: {dataset_path}", flush=True)
        if not dataset_path.is_file():
            raise PipelineFailed("Draft 평가셋 생성", f"준비 단계가 끝났지만 Draft JSON이 없습니다: {dataset_path}")

    print("\n[통합 2/2] Draft 검색 평가", flush=True)
    if report_path.exists() and not args.overwrite:
        if check_report(report_path, chunks_path, dataset_path):
            print(f"기존 검색 평가 결과 재사용: {report_path}", flush=True)
            print("통합 파이프라인 실행 완료", flush=True)
            return 0
        raise PipelineFailed(
            "기존 평가 결과 확인",
            f"기존 결과 파일이 현재 입력과 일치하지 않습니다: {report_path} "
            "검토 후 --overwrite로 갱신하세요.",
        )

    command = [sys.executable, str(EVAL_SCRIPT), *args_common]
    if args.external:
        command.extend(["--dataset-path", str(dataset_path), "--output-path", str(report_path)])
    if args.overwrite:
        command.append("--overwrite")

    # 평가가 성공했더라도 임시 Chroma 잠금으로 자식의 삭제가 실패할 수 있다.
    # 이 경우 결과 구조+해시 확인 후, 자식 종료 시점에 부모의 임시 폴더 정리를 이용한다.
    before_mtime = report_path.stat().st_mtime_ns if report_path.is_file() else None
    with TemporaryDirectory(prefix="xml_full_parent_") as scratch:
        env = os.environ.copy()
        env["TMP"] = scratch
        env["TEMP"] = scratch
        result = run_command(command, pipeline="eval", env=env)

        valid = check_report(report_path, chunks_path, dataset_path)
        after_mtime = report_path.stat().st_mtime_ns if report_path.is_file() else None
        newly_written = valid and after_mtime is not None and (before_mtime is None or after_mtime != before_mtime)
        if result.returncode != 0:
            if not newly_written or not result.cleanup_winerror32:
                raise PipelineFailed(result.last_stage, result.error_message, result.returncode)
            print(
                "\n주의: 결과 JSON은 정상 생성됐으나 임시 Chroma 삭제에서 WinError 32가 발생했습니다. "
                "자식 프로세스 종료 후 임시 폴더를 정리합니다.",
                flush=True,
            )
        elif not valid:
            raise PipelineFailed("평가 결과 검증", "평가 완료 후 JSON 구조 또는 입력 파일 해시 검증에 실패했습니다.")

    print(f"\n평가 결과 확인 완료: {report_path}", flush=True)
    print("통합 파이프라인 실행 완료", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineFailed as error:
        print("\n========== 파이프라인 실행 실패 ==========", file=sys.stderr, flush=True)
        print(f"실패 단계 (마지막 확인): {error.stage}", file=sys.stderr, flush=True)
        print(f"오류 원인: {error}", file=sys.stderr, flush=True)
        if error.returncode is not None:
            print(f"프로세스 종료 코드: {error.returncode}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
    except (OSError, ValueError, RuntimeError) as error:
        print("\n========== 파이프라인 실행 실패 ==========", file=sys.stderr, flush=True)
        print("실패 단계: 통합 실행기 내부 처리", file=sys.stderr, flush=True)
        print(f"오류 유형: {type(error).__name__}", file=sys.stderr, flush=True)
        print(f"오류 원인: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
