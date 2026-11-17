"""조사 담당자의 첫 번째 조사 단계에서 문서 검색 도구를 실행합니다."""

from pathlib import Path

from fin_research.tools.doc_search_tool import search_documents


def execute_search(question: str, persist_directory: Path, top_k: int) -> list[dict]:
    return search_documents(question, persist_directory, top_k)
