"""aggregate 노드: 판정 결과를 모아 요약한다.

병렬 verify_claim은 끝나는 순서대로 verdicts를 이어 붙이므로, 여기서
① 주장 순서(C1, C2 …)로 다시 정렬하고 ② 같은 주장의 중복 판정은 하나만 남기고
③ 판정이 빠진 주장이 있으면 not_found로 채운 뒤 ④ 판정별 개수를 만든다.
LLM은 쓰지 않는다.
"""
from __future__ import annotations

from fin_research.verify.state import Verdict, VerifyState, summarize


def order_verdicts(claim_ids: list[str], verdicts: list[Verdict]) -> tuple[list[Verdict], list[str]]:
    """(주장 순서대로 정렬한 판정, 안내 메시지)."""
    by_claim: dict[str, Verdict] = {}
    for verdict in verdicts:
        by_claim.setdefault(verdict.claim_id, verdict)  # 중복이면 처음 것
    notes: list[str] = []
    ordered: list[Verdict] = []
    for claim_id in claim_ids:
        verdict = by_claim.pop(claim_id, None)
        if verdict is None:
            verdict = Verdict(claim_id=claim_id, label="not_found", explanation="검증 결과가 없습니다")
            notes.append(f"aggregate: {claim_id}의 검증 결과가 없어 not_found로 처리했습니다.")
        ordered.append(verdict)
    if by_claim:  # 주장 목록에 없는 판정 (있으면 안 되지만 숨기지 않는다)
        notes.append(f"aggregate: 주장 목록에 없는 판정 {sorted(by_claim)}을 제외했습니다.")
    return ordered, notes


def aggregate_node(state: VerifyState) -> dict:
    claim_ids = [claim.id for claim in state.get("claims") or []]
    ordered, notes = order_verdicts(claim_ids, state.get("verdicts") or [])
    update: dict = {
        "verdicts": {"type": "override", "value": ordered},
        "summary": summarize(ordered),
        "step": "aggregate",
    }
    # companies.yaml에 접수번호가 없는 기업은 DB 기준값이 나온 공시 번호로 채운다
    if not state.get("rcept_no"):
        db_rcept = next((v.rcept_no for v in ordered if v.rcept_no), None)
        if db_rcept:
            update["rcept_no"] = db_rcept
    if notes:
        update["errors"] = notes
    return update
