
import json
import re
from pathlib import Path

from langchain_core.documents import Document
from langchain_community.retrievers import BM25Retriever

from fin_research.retrieval.search_target import SearchTarget


# 간단한 한국어·영문·숫자 토큰화
def simple_tokens(text: str) -> list[str]:
    return re.findall(r"[가-힣A-Za-z0-9]+", text.lower())


# 청크 JSON을 LangChain Document로 변환
def load_chunk_documents(chunks_path: str | Path) -> list[Document]:
    chunks_path = Path(chunks_path)

    with chunks_path.open("r", encoding="utf-8") as file:
        chunks = json.load(file)

    if not isinstance(chunks, list) or not chunks:
        raise ValueError("청크 JSON은 비어 있지 않은 배열이어야 합니다.")

    documents = []

    for chunk in chunks:
        required_fields = {
            "chunk_id", "text", "company", "fiscal_year",
            "statement_scope", "source_file", "pdf_page", "section"
        }

        if not isinstance(chunk, dict):
            raise ValueError("청크 데이터는 JSON 객체여야 합니다.")

        missing = required_fields - chunk.keys()
        if missing:
            raise ValueError(f"청크 필드 누락: {sorted(missing)}")

        text = chunk["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"빈 청크: {chunk['chunk_id']}")

        metadata = {
            key: value for key, value in chunk.items()
            if key != "text"
        }

        documents.append(
            Document(
                page_content=text,
                metadata=metadata
            )
        )

    return documents


# BM25Retriever 생성 (수업 방식 재사용)
def build_bm25_retriever(
    documents: list[Document],
    k: int = 5
) -> BM25Retriever:
    if not documents:
        raise ValueError("검색할 문서가 없습니다.")

    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")

    retriever = BM25Retriever.from_documents(
        documents,
        preprocess_func=simple_tokens
    )

    retriever.k = k
    return retriever


# 질문을 받아 BM25 검색 결과 반환
def search_bm25(
    retriever: BM25Retriever,
    question: str
) -> list[dict]:
    if not question.strip():
        raise ValueError("검색 질문이 비어 있습니다.")

    hits = retriever.invoke(question)

    results = []

    for rank, document in enumerate(hits, start=1):
        metadata = document.metadata

        results.append({
            "rank": rank,
            "chunk_id": metadata["chunk_id"],
            "text": document.page_content,
            "source_file": metadata["source_file"],
            "pdf_page": metadata["pdf_page"],
            "metadata": metadata
        })

    return results


def search_bm25_for_target(
    target: SearchTarget,
    project_root: str | Path,
    question: str,
    k: int = 5,
) -> list[dict]:
    """지정한 기업·공시·재무제표 기준으로 BM25 검색한다."""

    # 1. 검색 대상 청크 경로 결정
    chunks_path = target.chunks_path(project_root)

    # 2. 청크 파일 존재 여부 확인
    if not chunks_path.is_file():
        raise FileNotFoundError(
            f"검색 대상 청크 파일이 없습니다: {chunks_path}"
        )

    # 3. 기존 청크 로더 재사용
    documents = load_chunk_documents(chunks_path)

    # 4. 선택한 검색 대상과 청크 메타데이터 비교
    expected_source = f"{target.rcept_no}.pdf"

    for document in documents:
        metadata = document.metadata

        if metadata["fiscal_year"] != target.fiscal_year:
            raise ValueError(
                f"사업연도 불일치: {metadata['chunk_id']}"
            )

        if metadata["statement_scope"] != target.statement_scope:
            raise ValueError(
                f"연결/별도 기준 불일치: {metadata['chunk_id']}"
            )

        if metadata["source_file"] != expected_source:
            raise ValueError(
                f"원본 PDF 불일치: {metadata['chunk_id']}"
            )

    # 5. 기존 BM25 검색기 생성
    retriever = build_bm25_retriever(
        documents=documents,
        k=k,
    )

    # 6. 검색 결과 반환
    return search_bm25(
        retriever=retriever,
        question=question,
    )
