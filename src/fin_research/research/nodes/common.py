"""research 노드 공용 도우미."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

from fin_research.llm.client import PROJECT_ROOT

if TYPE_CHECKING:
    from fin_research.llm.budget import Budget

PROMPT_DIR = PROJECT_ROOT / "configs" / "prompts" / "research"


def get_budget(config: RunnableConfig | None) -> Budget | None:
    """config["configurable"]["budget"] (없으면 None)."""
    return ((config or {}).get("configurable") or {}).get("budget")


def load_prompt(name: str) -> str:
    """configs/prompts/research/{name}.md 내용. 비어 있으면 에러 (프롬프트 누락을 바로 알기 위해)."""
    path = PROMPT_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"프롬프트 파일이 비어 있습니다: {path}")
    return text
