"""보고서 실행 단위의 전역 출처 번호 발급과 중복 제거."""

from __future__ import annotations

from collections.abc import Mapping
from threading import RLock
from typing import Any

from fin_research.evidence.models import Evidence, EvidenceCandidate


class EvidenceRegistry:
    """하나의 보고서에서 사용한 근거를 ``S#`` 번호로 관리한다.

    ``register``는 같은 원본 파일의 같은 청크를 다시 받으면 기존 번호를
    반환한다. 서로 다른 하위 질문이 동시에 등록하더라도 번호가 중복 발급되지
    않도록 잠금으로 보호한다.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Evidence] = {}
        self._id_by_source_key: dict[tuple[str, str], str] = {}
        self._lock = RLock()

    def register(self, candidate: EvidenceCandidate | Mapping[str, Any]) -> str:
        """근거를 등록하고 전역 출처 ID를 반환한다.

        ``candidate``에는 XML 검색 결과처럼 ``source_file``, ``source_xpath``,
        ``chunk_id``, ``text``를 담은 딕셔너리를 그대로 넘길 수 있다.
        """

        parsed = self._parse_candidate(candidate)
        with self._lock:
            existing_id = self._id_by_source_key.get(parsed.source_key)
            if existing_id is not None:
                return existing_id

            evidence_id = f"S{len(self._by_id) + 1}"
            evidence = Evidence(evidence_id=evidence_id, **parsed.model_dump())
            self._by_id[evidence_id] = evidence
            self._id_by_source_key[parsed.source_key] = evidence_id
            return evidence_id

    def get(self, evidence_id: str) -> Evidence | None:
        """출처 ID로 등록된 근거를 조회한다. 없으면 ``None``을 반환한다."""

        with self._lock:
            return self._by_id.get(evidence_id)

    def require(self, evidence_id: str) -> Evidence:
        """출처 ID로 근거를 조회한다. 없으면 명확한 오류를 낸다."""

        evidence = self.get(evidence_id)
        if evidence is None:
            raise KeyError(f"등록되지 않은 출처 ID입니다: {evidence_id}")
        return evidence

    def values(self) -> tuple[Evidence, ...]:
        """번호 발급 순서대로 등록된 모든 근거를 반환한다."""

        with self._lock:
            return tuple(self._by_id.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)

    def related(self, context_key: str, question: str, limit: int = 5):
        """동일 조사 기준에서 확보한 문서만 제목·핵심어로 골라 다른 질문에 재사용한다."""
        from fin_research.retrieval.bm25_retriever import simple_tokens
        topics = [w for w in ("매출", "영업이익", "순이익", "원인", "소송", "우발", "감사", "현금", "자본") if w in question]
        tokens = set(simple_tokens(question)) - {"연결", "별도", "기준", "설명", "관련", "대한"}
        def score(item):
            """같은 기준의 근거 안에서 질문과 겹치는 실제 문구를 점수화한다."""
            text = (item.title or "") + " " + item.text
            return 3 * sum(w in text for w in topics) + sum(t in text for t in tokens)
        candidates = [e for e in self.values() if e.context_key == context_key and e.source_format not in ("db", "calc")]
        return tuple(e for e in sorted(candidates, key=score, reverse=True) if score(e) > 0)[:limit]

    @staticmethod
    def _parse_candidate(candidate: EvidenceCandidate | Mapping[str, Any]) -> EvidenceCandidate:
        if isinstance(candidate, EvidenceCandidate):
            return candidate
        return EvidenceCandidate.model_validate(candidate)
