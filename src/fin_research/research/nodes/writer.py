
from __future__ import annotations

import asyncio
import re
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import Command

from fin_research.llm.budget import BudgetExceeded
from fin_research.llm.router import get_llm_for_role
from fin_research.research.nodes.common import get_budget, load_prompt
from fin_research.research.nodes.outline import CONCLUSION_TITLE, SUMMARY_TITLE
from fin_research.research.nodes.supervisor import latest_by_subtopic
from fin_research.research.state import Finding, OutlineSection, ResearchState


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CITATION = re.compile(r"\[([^\[\]]*?\bS\d+\b[^\[\]]*?)\]")
_SOURCE_ID = re.compile(r"\bS\d+\b")

# 검사

def numbers_in(text:str)->set[str]:
    """비교용 숫자: 쉼표를 빼고 한 자리숫자('2문장','3개')는 제외한다"""
    found=set()
    for raw in _NUMBER.findall(text):
        number=raw.replace(',','').rstrip('.')
        if len(number.replace('.',''))>=2:
            found.add(number)
    return found
def citations_in(text:str)-> set[str]:
    return {i for group in _CITATION.findall(text) for i in _SOURCE_ID.findall(group)}

def check_section(written: str, source_text:str, allowed_ids: set[str], allow_numbers: bool = True)->list[str]:
    """쓴 문장의 문제 목록. 비어 있으면 통과. allow_numbers=False면(결론) 숫자를 쓰면 안 된다."""
    problems=[]
    if not allow_numbers and numbers_in(written):
        problems.append(f"결론에 숫자 사용:{','.join(sorted(numbers_in(written)))}")
    new_numbers=numbers_in(written)- numbers_in(source_text)
    if new_numbers:
        problems.append(f"입력에 없는 숫자:{','.join(sorted(new_numbers))}")
    bad_ids=citations_in(written)- allowed_ids
    if bad_ids:
        problems.append(f"입력에 없는 출처:{','.join(sorted(bad_ids))}")
    if not written.strip():
        problems.append("빈 답변")
    return problems

# 입력 만들기
def section_source(findings:list[Finding])-> str:
    """LLM에 줄 조사 결과·근거 조각·한계. 검사도 이 글을 기준으로 한다."""

    parts=[]
    for finding in findings:
        parts.append(f"[조사 결과]\n{finding.summary}")
        quotes=[f"{s.id}: {s.quote}" for s in finding.sources if s.quote]
        if quotes:
            parts.append("[근거 조각]\n"+"\n".join(quotes))
        if finding.limitations:
            parts.append("[한계]\n" + "\n".join(f"- {note}" for note in finding.limitations))
        
    return "\n\n".join(parts)

def insufficient_text(findings: list[Finding])->str:
    """근거가 없는 섹션: LLM을 부르지 않고 코드가 쓴다."""

    notes=[note for f in findings for note in f.limitations][:2]

    detail=f" ({'; '.join(notes)})" if notes else ""
    return f"확보한 공시 근거로 이 항목을 확인하지 못했습니다.{detail}"

def conclusion_fallback(done_titles: list[str], missing_titles: list[str]) -> str:
    """결론 검사 실패 시 코드가 쓰는 숫자 없는 결론."""
    text = f"{', '.join(done_titles)} 항목은 공시 근거로 확인했으며, 세부 수치는 각 섹션에 정리했습니다."
    if missing_titles:
        text += f" {', '.join(missing_titles)} 항목은 확인하지 못했습니다."
    return text

# LLM 호출

async def write_one(llm,system:str,title: str, source: str, company: str, years: str)-> str:
    messages=[
        SystemMessage(system),
        HumanMessage(f"기업: {company}\n사업연도: {years}\n섹션: {title}\n\n{source}"),
    ]
    response=await llm.ainvoke(messages)
    return str(response.content).strip()

async def write_checked(llm, system,title,source, allowed_ids,fallback,company,years, allow_numbers=True):
    """(본문, 경고). LLM 실패·검사 실패면 fallback 문장을 쓴다."""
    try:
        written=await write_one(llm,system,title,source,company,years)
    except BudgetExceeded as e:
        return  fallback, f"writer {title}: [예산 초과] 조사 결과 문장을 그대로 사용 ({e})"
    problems=check_section(written,source,allowed_ids,allow_numbers)
    if problems:
        excerpt = " ".join(written.split())[:120]
        return fallback,f"writer {title}: {'; '.join(problems)} → 대체 문장 사용 (LLM 답변: {excerpt})"
    return written,None


# 노드

async def writer_node(state:ResearchState,config:RunnableConfig)->Command[Literal["__end__"]]:
    brief=state['brief']
    outline=state['outline']
    latest=latest_by_subtopic(state.get('findings') or [])
    years=",".join(f'{y}년' for y in brief.years)
    llm=get_llm_for_role("writer",budget=get_budget(config))

    system=load_prompt("writer")
    def findings_of(section: OutlineSection)->list[Finding]:
        return [latest[i] for i in section.subtopic_ids if i in latest]
    body=[s for s in outline.sections if s.title not in (SUMMARY_TITLE,CONCLUSION_TITLE)]
    written:dict[str,str]={}
    warnings:list[str]=[]
# ① 주제 섹션: 근거가 있는 섹션만 동시에 LLM
    async def body_section(section: OutlineSection)-> tuple[str,str,str | None]:
        findings = findings_of(section)
        if not section.evidence_ids:
            return section.title, insufficient_text(findings), None
        source = section_source(findings)
        fallback = "\n".join(f.summary for f in findings)
        text, warning = await write_checked(llm, system, section.title, source, set(section.evidence_ids),
                                            fallback, brief.company, years)
        return section.title, text, warning

    for title, text, warning in await asyncio.gather(*(body_section(s) for s in body)):
        written[title] = text
        if warning:
            warnings.append(warning)

    # ② 요약·결론: 검사를 통과한 섹션 문장만 입력으로
    done = [s for s in body if s.evidence_ids]
    body_text = "\n\n".join(f"[{s.title}]\n{written[s.title]}" for s in done)
    missing = [s.title for s in body if not s.evidence_ids]
    if missing:  # 근거 없는 주제를 알려 줘야 결론에서 '안정적' 같은 평가를 지어내지 않는다
        body_text += "\n\n[확인하지 못한 주제]\n" + ", ".join(missing)
    allowed = {e for s in done for e in s.evidence_ids}
    for title in (SUMMARY_TITLE, CONCLUSION_TITLE):
        if not done:
            written[title] = "확보한 공시 근거가 없어 작성하지 못했습니다."
            continue
        if title == CONCLUSION_TITLE:
            fallback = conclusion_fallback([s.title for s in done], missing)
        else:
            fallback = " ".join(written[s.title].split(". ")[0].rstrip(".") + "." for s in done)
        text, warning = await write_checked(llm, system, title, body_text, allowed, fallback,
                                            brief.company, years, allow_numbers=title != CONCLUSION_TITLE)
        written[title] = text
        if warning:
            warnings.append(warning)

    # ③ 조립: outline 순서대로
    lines = [f"# {brief.company} 재무 분석 보고서 ({years})"]
    for section in outline.sections:
        lines += ["", f"## {section.title}", "", written[section.title]]
    update: dict = {"report": "\n".join(lines) + "\n", "step": "writer"}
    if warnings:
        update["errors"] = warnings
    return Command(update=update, goto=END)