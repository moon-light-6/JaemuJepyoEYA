"""조사 담당자가 기존 KURE-v1·Chroma 검색기를 사용할 수 있도록 연결합니다."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fin_research.retrieval.search_target import SearchTarget


class DocumentSearchTool:
    """한 번의 조사에서 임베딩 모델과 검색 저장소를 재사용합니다.

    target(SearchTarget)을 주면 그 기업·연도·공시의 컬렉션을 검색합니다
    (scripts/storage/build_chroma.py가 만드는 기업별 컬렉션). 주지 않으면 기존 단일 컬렉션을 씁니다.
    source="xml"이면 XML 청크 컬렉션(fin_xml_...)을 검색합니다 (scripts/pipelines/run_pipeline_xml.py가 만드는 색인).
    client(chromadb 클라이언트)를 주면 로컬 폴더 대신 그 서버(예: 팀 공용 ChromaDB)의 컬렉션을 검색합니다.
    """

    def __init__(self, persist_directory: Path, target: SearchTarget | None = None, source: str = "pdf", client=None):
        """선택한 형식과 대상 컬렉션을 고정하고 임베딩 로딩을 지연한다."""
        if source not in ("pdf", "xml") or (source == "xml" and target is None):
            raise ValueError("검색 형식과 XML 대상 기업·공시를 확인하세요.")
        self.persist_directory = Path(persist_directory)
        self.target = target
        self.source, self.client = source, client
        self._vectorstore = None

    @property
    def collection_name(self) -> str | None:
        return (self.target.xml_collection_name if self.source == "xml" else self.target.collection_name) if self.target else None

    def _collection_kwargs(self) -> dict:
        # target이 없으면 retriever의 기본 컬렉션을 그대로 쓰도록 인자를 넘기지 않는다
        return {"collection_name": self.collection_name} if self.target else {}

    def search(self, question: str, top_k: int = 5) -> list[dict]:
        if not question.strip():
            raise ValueError("검색 질문이 비어 있습니다.")
        if not 1 <= top_k <= 10:
            raise ValueError("top_k는 1~10 사이여야 합니다.")
        if self.client is None and not (self.persist_directory / "chroma.sqlite3").is_file():
            raise FileNotFoundError(
                f"Chroma 저장소가 없습니다: {self.persist_directory}. "
                "scripts/storage/build_chroma.py를 먼저 실행하세요."
            )

        # 명령어 도움말 조회 시에는 임베딩 모델을 불러오지 않습니다.
        from fin_research.retrieval.chroma_retriever import create_chroma, search_chroma

        if self._vectorstore is None:
            self._vectorstore = (_remote_chroma(self.client, self.collection_name) if self.client is not None else create_chroma(self.persist_directory, **self._collection_kwargs()))
        results = search_chroma(
            self.persist_directory, question, k=top_k, vectorstore=self._vectorstore,
            **self._collection_kwargs(),
        )
        return [
            {**result, "evidence_id": f"S{index}"}
            for index, result in enumerate(results, start=1)
        ]


def search_documents(
    question: str,
    persist_directory: Path,
    top_k: int = 5,
    target: SearchTarget | None = None,
    source: str = "pdf",
) -> list[dict]:
    """기존 색인을 사용해 검색하고, 각 청크에 출처 ID를 붙여 반환합니다."""
    return DocumentSearchTool(persist_directory, target, source).search(question, top_k)


class XMLDocumentSearchTool(DocumentSearchTool):
    """XML 원문의 출처를 유지하고 재검색에서는 키워드 검색을 함께 수행한다."""

    def __init__(self, persist_directory: Path, target: SearchTarget, *, section: str | None = None,
                 chunk_version: int = 1, project_root: Path | None = None, client=None, client_provider=None):
        """선택한 버전의 기존 색인을 열며 v2는 청크 내용의 해시로 저장소를 찾는다."""
        if chunk_version not in (1, 2):
            raise ValueError("XML 청크 버전은 1 또는 2여야 합니다.")
        directory = Path(persist_directory)
        if chunk_version == 2:
            import hashlib
            root = project_root or Path(__file__).resolve().parents[3]
            chunks = target.xml_scope_dir(root) / f"{target.rcept_no}_xml_chunks_v2.json"
            if not chunks.is_file():
                raise FileNotFoundError(f"v2 청크가 없습니다: {chunks}")
            directory /= hashlib.sha256(chunks.read_bytes()).hexdigest()
        super().__init__(directory, target, "xml", client=client)
        self.section, self.chunk_version = section, chunk_version
        self.client_provider = client_provider
        self._calls, self._seen, self._lexical_cache = 0, set(), {}
        self.last_search_info = {}

    @property
    def collection_name(self) -> str:
        """PDF와 분리된 XML 전용 컬렉션 이름을 반환한다."""
        return self.target.xml_collection_name

    def _filter(self, section=None):
        """사업연도·연결 기준·문서 형식을 고정하고 선택한 섹션만 검색한다."""
        conditions = [{"source_format": "xml"}, {"fiscal_year": self.target.fiscal_year},
                      {"statement_scope": self.target.statement_scope}]
        if section:
            conditions.append({"section": section})
        return {"$and": conditions}

    def _documents(self, where):
        """필터가 일치하는 최대 5천 청크를 캐시하고 과다한 원문 로딩은 생략한다."""
        import json
        from langchain_core.documents import Document
        key = json.dumps(where, sort_keys=True, ensure_ascii=False)
        if key not in self._lexical_cache:
            rows = self._vectorstore._collection.get(where=where, include=["documents", "metadatas"], limit=5001)
            texts = rows.get("documents") if isinstance(rows, dict) else []
            metadata = rows.get("metadatas") if isinstance(rows, dict) else []
            documents = [Document(page_content=text, metadata=meta)
                         for text, meta in zip(texts or [], metadata or []) if text and meta]
            self._lexical_cache[key] = documents if len(documents) <= 5000 else []
        return self._lexical_cache[key]

    def _lexical_hits(self, question, where, top_k):
        """제목·핵심어를 우선하고 BM25도 결합해 조사·공시 공통어의 영향을 줄인다."""
        from fin_research.retrieval.bm25_retriever import build_bm25_retriever, simple_tokens
        documents = self._documents(where)
        if not documents:
            return []
        tokens = set(simple_tokens(question)) - {"기준", "연결", "별도", "관련", "대한", "내용", "설명", "현황"}
        topics = [word for word in ("소송", "우발", "약정", "감사의견", "내부통제", "경영진", "매출", "원인", "차입", "현금흐름") if word in question]
        if "우발" in topics:
            tokens.update(["우발채무", "우발부채"])
        def score(doc):
            """제목과 본문에서 질문의 주요 용어가 실제 등장하는 후보를 우선한다."""
            title, text = doc.metadata.get("title", ""), doc.page_content
            return sum(5 * (word in title) + 3 * (word in text) for word in topics) + sum(word in text for word in tokens)
        focused = sorted(documents, key=score, reverse=True)
        focused = [doc for doc in focused if score(doc) > 0][:top_k]
        bm25 = build_bm25_retriever(documents, k=top_k).invoke(question)
        combined = focused + [doc for doc in bm25 if tokens.intersection(simple_tokens(doc.page_content))]
        return list({doc.metadata["chunk_id"]: doc for doc in combined}.values())[:top_k]

    def _expand_neighbors(self, seeds, where, limit):
        """같은 XML·동일 원문 문서의 바로 앞뒤 청크만 제한적으로 확장한다."""
        documents = self._documents(where)
        by_id = {doc.metadata["chunk_id"]: doc for doc in documents}
        expanded, known = [], {doc.metadata["chunk_id"] for doc in seeds}
        for seed in seeds:
            identifier = seed.metadata["chunk_id"]
            group, _, suffix = identifier.rpartition("_")
            if not suffix.isdigit():
                continue
            for number in (int(suffix) - 1, int(suffix) + 1):
                key = f"{group}_{number:0{len(suffix)}d}"
                neighbor = by_id.get(key)
                if (neighbor and key not in known
                        and neighbor.metadata.get("source_file") == seed.metadata.get("source_file")
                        and neighbor.metadata.get("title") == seed.metadata.get("title")):
                    expanded.append(neighbor); known.add(key)
                if len(expanded) >= limit:
                    return expanded
        return expanded

    def search(self, question: str, top_k: int = 5) -> list[dict]:
        """첫 검색은 벡터, 이후 검색은 주석 우선 벡터·BM25 결합으로 새 근거를 찾는다."""
        if not question.strip() or not 1 <= top_k <= 10:
            raise ValueError("검색 질문과 1~10 범위의 top_k가 필요합니다.")
        if self.client is None and not (self.persist_directory / "chroma.sqlite3").is_file() and self.client_provider:
            self.client = self.client_provider()
        if self.client is None and not (self.persist_directory / "chroma.sqlite3").is_file():
            raise FileNotFoundError("선택한 버전의 XML Chroma 색인이 없습니다. XML 파이프라인으로 먼저 색인하세요.")
        from fin_research.retrieval.chroma_retriever import create_chroma
        from fin_research.retrieval.xml_retrieval import result_from_document
        if self._vectorstore is None:
            self._vectorstore = (_remote_chroma(self.client, self.collection_name) if self.client is not None else create_chroma(self.persist_directory, self.collection_name))
        if self._vectorstore._collection.count() == 0 and self.client is None and self.client_provider:
            self.client = self.client_provider()
            if self.client is not None:
                self._vectorstore = _remote_chroma(self.client, self.collection_name)
        if self._vectorstore._collection.count() == 0:
            raise ValueError("선택한 XML 컬렉션에 청크가 없습니다.")
        retry = self._calls > 0
        section = self.section
        if retry and not section and not any(word in question for word in ("감사", "내부통제", "내부회계", "경영진", "원인", "분석")):
            section = "연결재무제표 주석" if self.target.statement_scope == "consolidated" else "재무제표 주석"
        where = self._filter(section)
        count = min(top_k * 2, 20)
        hits = self._vectorstore.similarity_search_with_score(question, k=count, filter=where)
        if retry and not hits and not self.section:
            where = self._filter()
            hits = self._vectorstore.similarity_search_with_score(question, k=count, filter=where)
        ranked = [[(document, float(distance)) for document, distance in hits]]
        lexical = self._lexical_hits(question, where, count)
        ranked.append([(document, None) for document in lexical])
        scores, candidates = {}, {}
        prefix = f"{self.target.corp_code}_{self.target.fiscal_year}_{self.target.statement_scope}_xml_"
        for ranking in ranked:
            seen = set()
            for rank, (document, distance) in enumerate(ranking, 1):
                result = result_from_document(document, rank, distance=distance)
                identifier = result["chunk_id"]
                if not identifier.startswith(prefix) or not result["source_file"].startswith(self.target.rcept_no):
                    raise ValueError("XML 검색 결과의 기업 또는 접수번호가 다릅니다.")
                if identifier in seen:
                    continue
                seen.add(identifier)
                scores[identifier] = scores.get(identifier, 0) + (2 if ranking is ranked[-1] else 1) / (60 + rank)
                candidates.setdefault(identifier, result)
        identifiers = sorted(candidates, key=lambda key: (key in self._seen, -scores[key], key))[:top_k]
        # 검색 상위 근거는 유지하고 최대 top_k개의 인접 청크를 추가한다(총 2*top_k 한도).
        seed_docs = [doc for ranking in ranked for doc, _ in ranking
                     if doc.metadata["chunk_id"] in identifiers]
        neighbors = self._expand_neighbors(seed_docs, where, top_k)
        for doc in neighbors:
            key = doc.metadata["chunk_id"]
            if key not in candidates:
                candidates[key] = result_from_document(doc, 0)
            if key not in identifiers:
                identifiers.append(key)
        if any(not key.startswith(prefix) or not candidates[key]["source_file"].startswith(self.target.rcept_no)
               for key in identifiers):
            raise ValueError("XML 검색 결과의 기업 또는 접수번호가 다릅니다.")
        results = [{**candidates[key], "rank": rank, "pdf_page": None, "evidence_id": f"S{rank}",
                    "rrf_score": scores.get(key, 0), "neighbor_expansion": key not in scores}
                   for rank, key in enumerate(identifiers, 1)]
        self._seen.update(identifiers)
        self._calls += 1
        self.last_search_info = {"strategy": "hybrid_notes" if retry else "hybrid_title",
                                 "collection": self.collection_name, "chunk_version": self.chunk_version,
                                 "persist_directory": str(self.persist_directory), "filter": where,
                                 "candidate_count": len(candidates), "neighbor_count": len(neighbors)}
        return results


def _remote_chroma(client, collection_name: str | None):
    """원격 Chroma 서버의 컬렉션을 LangChain Chroma로 연다. 질문 임베딩은 로컬 KURE-v1로 만든다."""
    if not collection_name:
        raise ValueError("원격 Chroma를 검색하려면 target(컬렉션 이름)이 필요합니다.")
    from langchain_chroma import Chroma

    from fin_research.retrieval.chroma_retriever import create_embedding_model

    return Chroma(client=client, collection_name=collection_name, embedding_function=create_embedding_model())


def shared_chroma_client(host: str | None = None):
    """팀 공용 ChromaDB(.env의 CHROMA_SHARED_HOST, Tailscale Serve HTTPS 443) 클라이언트.

    주소가 없거나 서버가 꺼져 있으면 None. 호출하는 쪽은 로컬 색인만으로 계속 진행한다.
    """
    if host is None:
        from dotenv import load_dotenv

        from fin_research.llm.client import PROJECT_ROOT

        load_dotenv(PROJECT_ROOT / ".env")
        host = os.getenv("CHROMA_SHARED_HOST")
    if not host:
        return None
    try:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.HttpClient(host=host, port=443, ssl=True,
                                     settings=Settings(anonymized_telemetry=False))
        client.heartbeat()
        return client
    except Exception:  # 꺼져 있음·Tailscale 미연결·주소 오류: 공용 색인 없이 진행
        return None
