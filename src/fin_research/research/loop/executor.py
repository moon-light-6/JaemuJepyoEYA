"""Execute the document-search action for the first Researcher iteration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fin_research.tools.doc_search_tool import search_documents

if TYPE_CHECKING:
    from fin_research.retrieval.search_target import SearchTarget


def execute_search(
    question: str, persist_directory: Path, top_k: int, target: SearchTarget | None = None,
) -> list[dict]:
    return search_documents(question, persist_directory, top_k, target)
