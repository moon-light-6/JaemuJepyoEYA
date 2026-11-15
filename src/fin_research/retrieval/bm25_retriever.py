
import json
import re
from pathlib import Path

from langchain_core.documents import Document
from langchain_community.retrievers import BM25Retriever


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
