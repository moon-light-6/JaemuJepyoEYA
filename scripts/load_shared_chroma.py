
import argparse
import hashlib
import json
import sys
from pathlib import Path

import chromadb
from chromadb.errors import NotFoundError
from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fin_research.retrieval.search_target import SearchTarget
from fin_research.retrieval.xml_retrieval import load_xml_chunk_documents


def main():
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser()
    parser.add_argument("--corp-code", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--scope", required=True,
                        choices=["consolidated", "separate"])
    parser.add_argument("--rcept-no", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    target = SearchTarget(
        args.corp_code, args.year, args.scope, args.rcept_no
    )

    path = (
        ROOT / "data/processed/xml/dart"
        / args.corp_code / str(args.year) / args.scope
        / f"{args.rcept_no}_xml_chunks_v2.json"
    )
    if not path.is_file():
        raise FileNotFoundError(path)

    docs = load_xml_chunk_documents(path)
    ids = [str(d.metadata["chunk_id"]) for d in docs]
    expected = {str(d.metadata["chunk_id"]): d.page_content
                for d in docs}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    name = target.xml_collection_name
    local_dir = ROOT / "data/chroma_xml_v2" / digest

    # 기존 임베딩 조회: 로컬 DB에 새로 저장하지 않음
    local_vectors = {}
    if (local_dir / "chroma.sqlite3").is_file():
        local = chromadb.PersistentClient(
            path=str(local_dir)
        ).get_collection(name, embedding_function=None)

        data = local.get(
            include=["embeddings", "documents", "metadatas"]
        )

        if set(data["ids"]) != set(ids):
            raise ValueError("로컬 Chroma ID 불일치")

        if data["embeddings"] is None:
            raise ValueError("로컬 임베딩을 읽지 못했습니다.")

        for cid, vector, text, meta in zip(
            data["ids"], data["embeddings"],
            data["documents"], data["metadatas"]
        ):
            if expected[cid] != text:
                raise ValueError(f"로컬 본문 불일치: {cid}")
            local_vectors[cid] = (
                vector.tolist(), text, meta
            )

    host = os.getenv("CHROMA_SHARED_HOST")
    if not host:
        raise ValueError("CHROMA_SHARED_HOST 설정 필요")

    client = chromadb.HttpClient(
        host=host, port=443, ssl=True
    )
    client.heartbeat()

    try:
        remote = client.get_collection(
            name, embedding_function=None
        )
    except NotFoundError:
        remote = None

    existing = {}
    if remote is not None:
        saved_hash = (remote.metadata or {}).get("chunks_sha256")
        if saved_hash and saved_hash != digest:
            raise ValueError("공용 컬렉션의 원본 버전이 다릅니다.")

        result = remote.get(include=["documents"])
        existing = dict(zip(
            result["ids"], result["documents"]
        ))

        if set(existing) - set(ids):
            raise ValueError("공용 DB에 다른 청크 ID가 있습니다.")

        for cid, text in existing.items():
            if expected[cid] != text:
                raise ValueError(f"공용 본문 충돌: {cid}")

    pending = [cid for cid in ids if cid not in existing]

    print(f"컬렉션: {name}")
    print(f"XML v2: {len(ids)}개")
    print(f"공용 저장: {len(existing)}개")
    print(f"추가 필요: {len(pending)}개")
    print(f"로컬 임베딩: {len(local_vectors)}개")

    if not args.apply:
        print("DRY-RUN: 실제 적재하지 않았습니다.")
        return

    if remote is None:
        remote = client.create_collection(
            name=name,
            embedding_function=None,
            metadata={
                "chunks_sha256": digest,
                "chunk_version": "v2",
                "embedding_model": "nlpai-lab/KURE-v1",
            },
        )

    model = None
    by_id = {
        str(d.metadata["chunk_id"]): d for d in docs
    }

    for start in range(0, len(pending), 32):
        batch = pending[start:start + 32]
        texts = [expected[cid] for cid in batch]

        if all(cid in local_vectors for cid in batch):
            vectors = [
                local_vectors[cid][0] for cid in batch
            ]
        else:
            if model is None:
                from fin_research.retrieval.chroma_retriever import (
                    create_embedding_model
                )
                model = create_embedding_model()
            vectors = model.embed_documents(texts)

        metadata = [
            {
                k: v for k, v in by_id[cid].metadata.items()
                if isinstance(v, (str, int, float, bool))
            }
            for cid in batch
        ]

        remote.add(
            ids=batch,
            documents=texts,
            metadatas=metadata,
            embeddings=vectors,
        )
        print(f"적재 진행: {min(start + 32, len(pending))}"
              f"/{len(pending)}")

    final = remote.get(include=["documents"])
    final_docs = dict(zip(
        final["ids"], final["documents"]
    ))

    if final_docs != expected:
        raise RuntimeError("최종 청크 ID 또는 본문 검증 실패")

    print(f"공용 적재 검증 완료: {len(final_docs)}개")
    print("Match: True")


if __name__ == "__main__":
    main()