"""Evidence Registry의 전역 S# 발급과 XML 청크 중복 제거를 검사한다.

프로젝트 루트에서 실행:
    python scripts/check_evidence_registry.py --corp-code 00260930
    python scripts/check_evidence_registry.py --corp-code 00260930 --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.evidence.registry import EvidenceRegistry


def load_chunks(path: Path) -> list[dict]:
    chunks = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("청크 파일은 비어 있지 않은 JSON 배열이어야 합니다.")
    return chunks


def check_chunk_file(path: Path, verbose: bool) -> dict[str, int]:
    """한 XML v2 청크 파일의 모든 S# 발급·재등록·조회 결과를 검사한다."""

    chunks = load_chunks(path)
    registry = EvidenceRegistry()
    expected_ids: dict[tuple[str, str], str] = {}
    xpath_to_ids: dict[tuple[str, str], set[str]] = {}
    xpath_to_chunk_ids: dict[tuple[str, str], set[str]] = {}

    for chunk in chunks:
        source_key = (chunk["source_file"], chunk["chunk_id"])
        evidence_id = registry.register(chunk)
        expected_id = expected_ids.setdefault(source_key, f"S{len(expected_ids) + 1}")
        if evidence_id != expected_id:
            raise ValueError(f"{path}: {chunk['chunk_id']}의 S# 순서가 올바르지 않습니다.")
        if registry.register(chunk) != evidence_id:
            raise ValueError(f"{path}: {chunk['chunk_id']} 재등록 시 S#이 달라졌습니다.")

        source = registry.require(evidence_id)
        for field in ("source_file", "chunk_id", "source_xpath", "text"):
            if getattr(source, field) != chunk[field]:
                raise ValueError(f"{path}: {evidence_id} 조회 결과의 {field} 값이 다릅니다.")

        xpath_key = (chunk["source_file"], chunk["source_xpath"])
        xpath_to_ids.setdefault(xpath_key, set()).add(evidence_id)
        xpath_to_chunk_ids.setdefault(xpath_key, set()).add(chunk["chunk_id"])
        if verbose:
            print(f"  {evidence_id} · {chunk['chunk_id']} · {chunk['source_xpath']}")

    if len(registry) != len(expected_ids):
        raise ValueError(f"{path}: 레지스트리 출처 수가 입력 청크와 다릅니다.")

    split_xpath_groups = 0
    for xpath_key, chunk_ids in xpath_to_chunk_ids.items():
        if len(chunk_ids) > 1:
            split_xpath_groups += 1
            if len(xpath_to_ids[xpath_key]) != len(chunk_ids):
                raise ValueError(f"{path}: 같은 XPath의 서로 다른 청크가 하나의 S#으로 합쳐졌습니다.")

    return {
        "chunks": len(chunks),
        "sources": len(registry),
        "reused": len(chunks),
        "lookups": len(chunks),
        "split_xpath_groups": split_xpath_groups,
    }


def check_chunk_files(paths: list[Path], heading: str, verbose: bool) -> None:
    if not paths:
        raise ValueError("검사할 XML v2 청크 파일이 없습니다.")

    print(f"=== {heading} ===")
    total_chunks = 0
    total_sources = 0
    for path in paths:
        result = check_chunk_file(path, verbose)
        total_chunks += result["chunks"]
        total_sources += result["sources"]
        print(f"✅ 검증 성공: {path.relative_to(PROJECT_ROOT)}")
        print(f"  1. {result['sources']}개 청크에 S1~S{result['sources']} 번호를 순서대로 발급했습니다.")
        print(f"  2. {result['reused']}개 청크를 다시 등록해도 처음 받은 S#가 유지됐습니다.")
        print(f"  3. {result['lookups']}개 S#를 조회했을 때 XML 파일·XPath·청크 ID·원문이 모두 일치했습니다.")
        print(f"  4. 같은 XPath에서 나뉜 청크 {result['split_xpath_groups']}개 그룹도 서로 다른 S#로 유지됐습니다.")

    print(f"\n결론: {len(paths)}개 파일의 {total_chunks}개 청크에 대해 {total_sources}개 S# 검증을 완료했습니다.")
def check_corp_code(corp_code: str, verbose: bool) -> None:
    if len(corp_code) != 8 or not corp_code.isdigit():
        raise ValueError("corp-code는 8자리 숫자 문자열이어야 합니다.")
    paths = sorted((PROJECT_ROOT / "data/processed/xml/dart" / corp_code).glob("*/*/*_xml_chunks_v2.json"))
    check_chunk_files(paths, f"기업 코드 {corp_code} XML v2 청크 검사", verbose)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evidence Registry XML 청크 검사")
    parser.add_argument("--corp-code", required=True, help="검사할 8자리 기업 코드")
    parser.add_argument("--verbose", action="store_true", help="청크별 S#·XPath를 모두 출력")
    args = parser.parse_args()

    check_corp_code(args.corp_code, args.verbose)


if __name__ == "__main__":
    main()
