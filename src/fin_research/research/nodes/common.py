"""research 노드 공용 도우미."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

from fin_research.llm.client import PROJECT_ROOT

if TYPE_CHECKING:
    from fin_research.llm.budget import Budget

PROMPTS_ROOT = PROJECT_ROOT / "configs" / "prompts"
PROMPT_DIR = PROMPTS_ROOT / "research"


def get_budget(config: RunnableConfig | None) -> Budget | None:
    """config["configurable"]["budget"] (없으면 None)."""
    return ((config or {}).get("configurable") or {}).get("budget")


def load_prompt(name: str, folder: str = "research") -> str:
    """configs/prompts/{folder}/{name}.md 내용. 비어 있으면 에러 (프롬프트 누락을 바로 알기 위해).

    verify 그래프는 folder="verify"로 같은 함수를 쓴다.
    """
    path = PROMPTS_ROOT / folder / f"{name}.md"
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"프롬프트 파일이 비어 있습니다: {path}")
    return text
