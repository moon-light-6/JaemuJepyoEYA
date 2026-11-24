"""research 노드 공용 도우미."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

from fin_research.llm.client import PROJECT_ROOT

if TYPE_CHECKING:
    from fin_research.evidence.registry import EvidenceRegistry
    from fin_research.llm.budget import Budget

PROMPTS_ROOT = PROJECT_ROOT / "configs" / "prompts"
PROMPT_DIR = PROMPTS_ROOT / "research"


def _configurable(config: RunnableConfig | None) -> dict:
    return (config or {}).get("configurable") or {}


def get_budget(config: RunnableConfig | None) -> Budget | None:
    """config["configurable"]["budget"] (없으면 None)."""
    return _configurable(config).get("budget")


def get_evidence_registry(config: RunnableConfig | None) -> EvidenceRegistry | None:
    """config["configurable"]["evidence_registry"] (없으면 None).

    실행(run) 하나에 장부 하나. 병렬 researcher가 같은 장부에 등록해야 같은 근거가 같은 S#가 된다.
    잠금·원문을 가지고 있어 state에 넣지 않는다 (Budget과 같은 이유).
    None이면 researcher는 장부 없이 임시 번호(profitability.r1.q1.S1)를 쓴다.
    """
    return _configurable(config).get("evidence_registry")


def load_prompt(name: str, folder: str = "research") -> str:
    """configs/prompts/{folder}/{name}.md 내용. 비어 있으면 에러 (프롬프트 누락을 바로 알기 위해).

    verify 그래프는 folder="verify"로 같은 함수를 쓴다.
    """
    path = PROMPTS_ROOT / folder / f"{name}.md"
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"프롬프트 파일이 비어 있습니다: {path}")
    return text
