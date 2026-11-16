"""Adapt the existing KURE-v1/Chroma search for a Researcher."""

from pathlib import Path


def search_documents(question: str, persist_directory: Path, top_k: int = 5) -> list[dict]:
    """Return source-labelled chunks without changing the existing index."""
    if not question.strip():
        raise ValueError("검색 질문이 비어 있습니다.")
    if not 1 <= top_k <= 10:
        raise ValueError("top_k는 1~10 사이여야 합니다.")
    if not (persist_directory / "chroma.sqlite3").is_file():
        raise FileNotFoundError("Chroma 저장소가 없습니다. scripts/build_chroma.py를 먼저 실행하세요.")

    # Import lazily so CLI help and GPT configuration checks do not load torch.
    from fin_research.retrieval.chroma_retriever import search_chroma

    results = search_chroma(persist_directory, question, k=top_k)
    return [
        {"evidence_id": f"S{index}", **result}
        for index, result in enumerate(results, start=1)
    ]
