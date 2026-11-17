"""BM25와 Chroma 검색 결과를 RRF로 결합하는 Hybrid 검색기."""

from pathlib import Path

from fin_research.retrieval.bm25_retriever import (
    load_chunk_documents,
    build_bm25_retriever,
    search_bm25,
)
from fin_research.retrieval.chroma_retriever import search_chroma
from fin_research.retrieval.search_target import SearchTarget


def rrf_merge(
    result_lists: list[list[dict]],
    top_k: int = 5,
    rank_constant: int = 60,
) -> list[dict]:
    """검색 결과들을 chunk_id 기준으로 병합하고 RRF 점수로 정렬한다."""

    if top_k <= 0:
        raise ValueError("top_k는 1 이상이어야 합니다.")

    if rank_constant < 0:
        raise ValueError("rank_constant는 0 이상이어야 합니다.")

    scores = {}
    documents = {}

    for results in result_lists:
        seen_ids = set()

        for rank, result in enumerate(results, start=1):
            chunk_id = result["chunk_id"]

            # 같은 검색기 내부 중복은 한 번만 반영
            if chunk_id in seen_ids:
                continue

            seen_ids.add(chunk_id)

            scores[chunk_id] = (
                scores.get(chunk_id, 0.0)
                + 1.0 / (rank_constant + rank)
            )

            if chunk_id not in documents:
                documents[chunk_id] = result

    sorted_ids = sorted(
        scores,
        key=lambda chunk_id: (
            -scores[chunk_id],
            chunk_id,
        ),
    )[:top_k]

    merged_results = []

    for rank, chunk_id in enumerate(sorted_ids, start=1):
        result = documents[chunk_id]

        merged_results.append({
            "rank": rank,
            "chunk_id": chunk_id,
            "text": result["text"],
            "source_file": result["source_file"],
            "pdf_page": result["pdf_page"],
            "rrf_score": scores[chunk_id],
            "metadata": result["metadata"],
        })

    return merged_results


def search_hybrid(
    target: SearchTarget,
    project_root: str | Path,
    persist_directory: str | Path,
    question: str,
    k: int = 5,
    candidate_k: int = 10,
    rank_constant: int = 60,
) -> list[dict]:
    """동일한 검색 대상으로 BM25와 Chroma 결과를 결합한다."""

    if not question.strip():
        raise ValueError("검색 질문이 비어 있습니다.")

    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")

    if candidate_k < k:
        raise ValueError("candidate_k는 k 이상이어야 합니다.")

    # 1. SearchTarget으로 청크 경로와 컬렉션 결정
    chunks_path = target.chunks_path(project_root)
    collection_name = target.collection_name

    # 2. BM25 대상 데이터 검증
    documents = load_chunk_documents(chunks_path)

    for document in documents:
        metadata = document.metadata

        if metadata["fiscal_year"] != target.fiscal_year:
            raise ValueError("사업연도가 일치하지 않습니다.")

        if metadata["statement_scope"] != target.statement_scope:
            raise ValueError("연결/별도 기준이 일치하지 않습니다.")

        if metadata["source_file"] != f"{target.rcept_no}.pdf":
            raise ValueError("공시 보고서가 일치하지 않습니다.")

    # 3. BM25 검색
    bm25_retriever = build_bm25_retriever(
        documents,
        k=candidate_k,
    )

    bm25_results = search_bm25(
        bm25_retriever,
        question,
    )

    # 4. 동일한 검색 대상의 Chroma 컬렉션 검색
    chroma_results = search_chroma(
        persist_directory=persist_directory,
        question=question,
        collection_name=collection_name,
        k=candidate_k,
    )

    # 5. Chroma 검색 결과도 동일한 보고서인지 검증
    for result in chroma_results:
        metadata = result["metadata"]

        if (
            metadata.get("fiscal_year") != target.fiscal_year
            or metadata.get("statement_scope") != target.statement_scope
            or metadata.get("source_file") != f"{target.rcept_no}.pdf"
        ):
            raise ValueError(
                "Chroma 검색 결과가 선택한 보고서와 일치하지 않습니다."
            )

    # 6. RRF 결합
    return rrf_merge(
        result_lists=[bm25_results, chroma_results],
        top_k=k,
        rank_constant=rank_constant,
    )
