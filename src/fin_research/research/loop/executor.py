"""Execute the document-search action for the first Researcher iteration."""

from pathlib import Path

from fin_research.tools.doc_search_tool import search_documents


def execute_search(question: str, persist_directory: Path, top_k: int) -> list[dict]:
    return search_documents(question, persist_directory, top_k)
