
import torch

from pathlib import Path

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

from fin_research.retrieval.bm25_retriever import load_chunk_documents


EMBEDDING_MODEL = "nlpai-lab/KURE-v1"
# 기존 루프와 이미 만들어진 단일 기업 색인의 호환성을 유지한다.
COLLECTION_NAME = "samsung_2025_consolidated_kure_v1"


def create_embedding_model():
    """KURE-v1 임베딩 모델을 로드한다."""

    if torch.cuda.is_available():
        device = "cuda"
    elif torch.backends.mps.is_available():
        device = "mps"
    elif hasattr(torch, "xpu") and torch.xpu.is_available():
        device = "xpu"
    else:
        device = "cpu"

    print(f"임베딩 실행 장치: {device}")

    return HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": device},
        encode_kwargs={"normalize_embeddings": True},
    )


def create_chroma(
    persist_directory: str | Path,
    collection_name: str = COLLECTION_NAME,
) -> Chroma:
    """지정한 이름의 로컬 Chroma 컬렉션을 생성하거나 다시 연다."""

    if not isinstance(collection_name, str) or not collection_name.strip():
        raise ValueError("컬렉션 이름은 비어 있지 않은 문자열이어야 합니다.")

    embedding_model = create_embedding_model()

    return Chroma(
        collection_name=collection_name,
        embedding_function=embedding_model,
        persist_directory=str(persist_directory),
        collection_metadata={"embedding_model": EMBEDDING_MODEL},
    )


def build_chroma(
    chunks_path: str | Path,
    persist_directory: str | Path,
    collection_name: str,
) -> int:
    """청크를 Chroma에 저장하고 저장된 문서 수를 반환한다."""

    documents = load_chunk_documents(chunks_path)

    # BM25와 동일한 청크 ID를 Chroma 문서 ID로 사용
    ids = [str(doc.metadata["chunk_id"]) for doc in documents]

    if len(ids) != len(set(ids)):
        raise ValueError("중복된 chunk_id가 있습니다.")

    # Chroma는 None 등 지원하지 않는 메타데이터 값에 민감하므로 기본적인 값만 남겨 저장
    for doc in documents:
        doc.metadata = {
            key: value
            for key, value in doc.metadata.items()
            if isinstance(value, (str, int, float, bool))
        }

    vectorstore = create_chroma(
        persist_directory=persist_directory,
        collection_name=collection_name,
    )

    existing_count = vectorstore._collection.count()

    if existing_count == 0:
        print(f"최초 색인 시작: {len(documents)}개 청크")

        batch_size = 32

        for start in range(0, len(documents), batch_size):
            end = start + batch_size

            vectorstore.add_documents(
                documents=documents[start:end],
                ids=ids[start:end],
            )

            print(
                f"색인 진행: "
                f"{min(end, len(documents))}/{len(documents)}"
            )

        print("최초 색인 완료")

    elif existing_count == len(documents):
        stored_ids = set(vectorstore.get(include=[])["ids"])

        if stored_ids != set(ids):
            raise ValueError(
                "기존 Chroma의 청크 ID가 현재 입력 데이터와 다릅니다. "
                "저장소와 입력 데이터를 확인하세요."
            )

        print("기존 Chroma 데이터 재사용")

    else:
        raise ValueError(
            f"Chroma 문서 수가 예상과 다릅니다. "
            f"기존={existing_count}, 입력={len(documents)}. "
            "중단된 색인 또는 다른 데이터가 들어 있을 수 있습니다."
        )

    final_count = vectorstore._collection.count()

    print(f"저장된 청크 수: {final_count}")

    return final_count


def search_chroma(
    persist_directory: str | Path,
    question: str,
    collection_name: str = COLLECTION_NAME,
    k: int = 5,
    *,
    vectorstore: Chroma | None = None,
) -> list[dict]:
    """Chroma에서 질문과 유사한 청크를 검색한다."""

    if not question.strip():
        raise ValueError("검색 질문이 비어 있습니다.")

    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")

    # 반복 조사에서는 이미 불러온 임베딩 모델과 저장소를 재사용합니다.
    if vectorstore is None:
        vectorstore = create_chroma(
            persist_directory=persist_directory,
            collection_name=collection_name,
        )
    # 색인이 수행되지 않았다면 검색하지 않음
    count = vectorstore._collection.count()

    if count == 0:
        raise ValueError(
            "Chroma에 저장된 청크가 없습니다. "
            "먼저 python scripts/build_chroma.py를 실행하세요."
        )

    # 수업의 similarity_search_with_score 활용
    hits = vectorstore.similarity_search_with_score(
        query=question,
        k=k,
    )

    results = []

    for rank, (document, distance) in enumerate(hits, start=1):
        metadata = document.metadata

        # BM25와 결과 형식 통일
        # Chroma에서만 추가로 distance 제공
        results.append({
            "rank": rank,
            "chunk_id": metadata["chunk_id"],
            "text": document.page_content,
            "source_file": metadata["source_file"],
            "pdf_page": metadata["pdf_page"],
            "distance": float(distance),
            "metadata": metadata,
        })

    return results


def search_mmr(
    persist_directory: str | Path,
    question: str,
    collection_name: str,
    k: int = 5,
    fetch_k: int = 20,
    lambda_mult: float = 0.5,
) -> list[dict]:
    """Chroma에서 관련성과 다양성을 고려해 MMR 검색한다."""

    if not question.strip():
        raise ValueError("검색 질문이 비어 있습니다.")

    if k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")

    if fetch_k < k:
        raise ValueError("fetch_k는 k 이상이어야 합니다.")

    if not 0.0 <= lambda_mult <= 1.0:
        raise ValueError("lambda_mult는 0~1 사이여야 합니다.")

    vectorstore = create_chroma(
        persist_directory=persist_directory,
        collection_name=collection_name,
    )

    if vectorstore._collection.count() == 0:
        raise ValueError(
            "Chroma에 저장된 청크가 없습니다. "
            "먼저 python scripts/build_chroma.py를 실행하세요."
        )

    # 수업에서 사용한 MMR Retriever 방식
    retriever = vectorstore.as_retriever(
        search_type="mmr",
        search_kwargs={
            "k": k,
            "fetch_k": fetch_k,
            "lambda_mult": lambda_mult,
        },
    )

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
            "metadata": metadata,
        })

    return results
