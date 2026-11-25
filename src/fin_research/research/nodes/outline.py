"""outline 노드: 보고서 목차와 섹션별 근거를 코드로 정한다 (LLM 없음).

계획서의 보고서 구성(요약 · 수익성 · 재무 안정성 · 현금흐름 · 리스크 · 결론)이 고정이라
목차를 LLM에게 맡기지 않는다. 로컬 LLM으로 바꿔도 목차가 흔들리지 않는다.

- 주제 섹션: 템플릿 순서 그대로, 그 주제의 최신 Finding 출처를 연결
- 조사에 실패한 주제도 섹션에 넣는다 (evidence_ids가 비어 있으면 writer가 '자료 부족'으로 쓴다)
- 요약·결론: 성공한 주제 전체를 근거로 가진다
"""
from __future__ import annotations

from typing import Literal

from langgraph.types import Command

from fin_research.research.nodes.supervisor import latest_by_subtopic
from fin_research.research.state import Outline, OutlineSection, ResearchState

SUMMARY_TITLE = "요약"
CONCLUSION_TITLE = "결론"


def build_outline(state: ResearchState) -> Outline:
    brief = state["brief"]
    latest = latest_by_subtopic(state.get("findings") or [])

    body: list[OutlineSection] = []
    for subtopic in brief.subtopics:  # 템플릿 순서 그대로
        finding = latest.get(subtopic.id)
        # partial(다중 도구 조사: 숫자는 확인, 설명은 미확인)도 확인된 근거가 있으면 쓴다
        usable = finding is not None and finding.status in ("draft", "partial")
        ids = finding.evidence_ids if usable else []
        body.append(OutlineSection(title=subtopic.title, subtopic_ids=[subtopic.id], evidence_ids=list(ids)))

    done_ids = [section.subtopic_ids[0] for section in body if section.evidence_ids]
    all_evidence = list(dict.fromkeys(e for section in body for e in section.evidence_ids))
    summary = OutlineSection(title=SUMMARY_TITLE, subtopic_ids=done_ids, evidence_ids=all_evidence)
    conclusion = OutlineSection(title=CONCLUSION_TITLE, subtopic_ids=done_ids, evidence_ids=all_evidence)
    return Outline(sections=[summary, *body, conclusion])


def outline_node(state: ResearchState) -> Command[Literal["writer"]]:
    return Command(update={"outline": build_outline(state), "step": "outline"}, goto="writer")
