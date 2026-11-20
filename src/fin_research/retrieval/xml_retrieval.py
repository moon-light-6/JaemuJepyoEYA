"""감사보고서 XML 청크를 위한 검색·평가 공통 기능.

PDF 검색기는 ``pdf_page``를 근거 위치로 사용한다. XML 검색기는 원본 XML 파일과
요소 XPath를 근거 위치로 사용하므로 PDF 전용 로더와 분리한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from fin_research.retrieval.chroma_retriever import create_chroma


XML_CHUNK_FIELDS = {
    "chunk_id", "text", "company", "fiscal_year", "statement_scope",
    "document_type", "source_format", "source_file", "source_xpath",
    "source_element_index", "section", "title",
}


def load_xml_chunk_documents(chunks_path: str | Path) -> list[Document]:
    """XML 청크 JSON을 LangChain Document 목록으로 바꾼다."""

    chunks_path = Path(chunks_path)
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("XML 청크 JSON은 비어 있지 않은 배열이어야 합니다.")

    documents: list[Document] = []
    seen_ids: set[str] = set()
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            raise ValueError(f"{index}번째 XML 청크가 JSON 객체가 아닙니다.")
        missing = XML_CHUNK_FIELDS - chunk.keys()
        if missing:
            raise ValueError(f"{index}번째 XML 청크 필드 누락: {sorted(missing)}")
        if chunk.get("source_format") != "xml":
            raise ValueError(f"{index}번째 청크의 source_format은 xml이어야 합니다.")
        if not isinstance(chunk["text"], str) or not chunk["text"].strip():
            raise ValueError(f"빈 XML 청크: {chunk['chunk_id']}")
        chunk_id = str(chunk["chunk_id"])
        if chunk_id in seen_ids:
            raise ValueError(f"중복된 XML chunk_id: {chunk_id}")
        seen_ids.add(chunk_id)
        documents.append(
            Document(
                page_content=chunk["text"],
                metadata={key: value for key, value in chunk.items() if key != "text"},
            )
        )
    return documents


def result_from_document(
    document: Document,
    rank: int,
    *,
    distance: float | None = None,
) -> dict[str, Any]:
    """검색 결과를 XML 근거 위치가 보이는 공통 형식으로 만든다."""

    metadata = document.metadata
    result: dict[str, Any] = {
        "rank": rank,
        "chunk_id": str(metadata["chunk_id"]),
        "text": document.page_content,
        "source_file": metadata["source_file"],
        "source_xpath": metadata["source_xpath"],
        "source_element_index": metadata["source_element_index"],
        "title": metadata["title"],
        "metadata": metadata,
    }
    if distance is not None:
        result["distance"] = distance
    return result


def build_xml_chroma(
    chunks_path: str | Path,
    persist_directory: str | Path,
    collection_name: str,
) -> int:
    """XML 전용 청크를 별도 Chroma 컬렉션에 색인한다."""

    documents = load_xml_chunk_documents(chunks_path)
    ids = [str(document.metadata["chunk_id"]) for document in documents]
    if len(ids) != len(set(ids)):
        raise ValueError("중복된 XML chunk_id가 있습니다.")
    for document in documents:
        document.metadata = {
            key: value for key, value in document.metadata.items()
            if isinstance(value, (str, int, float, bool))
        }
    vectorstore = create_chroma(persist_directory, collection_name)
    existing_ids = set(vectorstore.get(include=[])["ids"])
    if existing_ids and existing_ids != set(ids):
        raise ValueError("기존 XML Chroma 컬렉션의 청크 ID가 입력과 다릅니다. --rebuild-index를 사용하세요.")
    if not existing_ids:
        vectorstore.add_documents(documents, ids=ids)
    return vectorstore._collection.count()


def load_xml_eval_dataset(dataset_path: str | Path) -> list[dict[str, Any]]:
    """검증 완료된 XML 평가셋을 읽고 source_xpath 근거 형식을 검사한다."""

    dataset_path = Path(dataset_path)
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(dataset, list) or not dataset:
        raise ValueError("XML 평가셋은 비어 있지 않은 JSON 배열이어야 합니다.")

    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in dataset:
        if not isinstance(item, dict):
            raise ValueError("XML 평가 질문은 JSON 객체여야 합니다.")
        if item.get("annotation_status") != "verified":
            continue
        question_id = item.get("id")
        question = item.get("question")
        gold_sources = item.get("gold_sources")
        if not isinstance(question_id, str) or not question_id.strip():
            raise ValueError("XML 평가 질문 ID가 올바르지 않습니다.")
        if question_id in seen_ids:
            raise ValueError(f"중복된 XML 평가 질문 ID: {question_id}")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"{question_id}: 질문이 비어 있습니다.")
        if not isinstance(gold_sources, list) or not gold_sources:
            raise ValueError(f"{question_id}: XML Gold source가 없습니다.")
        for source in gold_sources:
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("source_file"), str)
                or not source["source_file"].strip()
                or not isinstance(source.get("source_xpath"), str)
                or not source["source_xpath"].strip()
            ):
                raise ValueError(
                    f"{question_id}: XML 출처는 source_file과 source_xpath를 가져야 합니다."
                )
        seen_ids.add(question_id)
        questions.append(item)
    if not questions:
        raise ValueError("검증 완료된 XML 평가 질문이 없습니다.")
    return questions


def is_xml_hit(results: list[dict[str, Any]], gold_sources: list[dict[str, Any]], k: int) -> bool:
    """상위 k개 검색 결과에 XML 파일·XPath가 모두 일치하는 근거가 있는지 검사한다."""

    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")
    gold_keys = {(source["source_file"], source["source_xpath"]) for source in gold_sources}
    return any(
        (result["source_file"], result["source_xpath"]) in gold_keys
        for result in results[:k]
    )


def is_xml_source_value_hit(
    results: list[dict[str, Any]],
    gold_sources: list[dict[str, Any]],
    expected_value: Any,
    k: int,
    contains_value,
) -> bool:
    """정답 XML 근거 위치와 정답 값이 같은 청크에 함께 있는지 검사한다."""

    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")
    gold_keys = {(source["source_file"], source["source_xpath"]) for source in gold_sources}
    return any(
        (result["source_file"], result["source_xpath"]) in gold_keys
        and contains_value(expected_value, result.get("text", ""))
        for result in results[:k]
    )

