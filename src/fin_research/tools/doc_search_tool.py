"""조사 담당자가 기존 KURE-v1·Chroma 검색기를 사용할 수 있도록 연결합니다."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fin_research.retrieval.search_target import SearchTarget


class DocumentSearchTool:
    """한 번의 조사에서 임베딩 모델과 검색 저장소를 재사용합니다.

    target(SearchTarget)을 주면 그 기업·연도·공시의 컬렉션을 검색합니다
    (scripts/storage/build_chroma.py가 만드는 기업별 컬렉션). 주지 않으면 기존 단일 컬렉션을 씁니다.
    """

    def __init__(self, persist_directory: Path, target: SearchTarget | None = None):
        self.persist_directory = Path(persist_directory)
        self.target = target
        self._vectorstore = None

    @property
    def collection_name(self) -> str | None:
        return self.target.collection_name if self.target else None

    def _collection_kwargs(self) -> dict:
        # target이 없으면 retriever의 기본 컬렉션을 그대로 쓰도록 인자를 넘기지 않는다
        return {"collection_name": self.collection_name} if self.target else {}

    def search(self, question: str, top_k: int = 5) -> list[dict]:
        if not question.strip():
            raise ValueError("검색 질문이 비어 있습니다.")
        if not 1 <= top_k <= 10:
            raise ValueError("top_k는 1~10 사이여야 합니다.")
        if not (self.persist_directory / "chroma.sqlite3").is_file():
            raise FileNotFoundError(
                f"Chroma 저장소가 없습니다: {self.persist_directory}. "
                "scripts/storage/build_chroma.py를 먼저 실행하세요."
            )

        # 명령어 도움말 조회 시에는 임베딩 모델을 불러오지 않습니다.
        from fin_research.retrieval.chroma_retriever import create_chroma, search_chroma

        if self._vectorstore is None:
            self._vectorstore = create_chroma(self.persist_directory, **self._collection_kwargs())
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
) -> list[dict]:
    """기존 색인을 사용해 검색하고, 각 청크에 출처 ID를 붙여 반환합니다."""
    return DocumentSearchTool(persist_directory, target).search(question, top_k)


class XMLDocumentSearchTool(DocumentSearchTool):
    """XML 원문의 출처를 유지하고 재검색에서는 키워드 검색을 함께 수행한다."""

    def __init__(self, persist_directory: Path, target: SearchTarget, *, section: str | None = None,
                 chunk_version: int = 1, project_root: Path | None = None):
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
        super().__init__(directory, target)
        self.section, self.chunk_version = section, chunk_version
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

    def _lexical_hits(self, question, where, top_k):
        """최대 5천 청크의 BM25 검색기를 재사용하고 초과 시 키워드 검색을 생략한다."""
        import json
        from langchain_core.documents import Document
        from fin_research.retrieval.bm25_retriever import build_bm25_retriever, simple_tokens
        key = json.dumps(where, sort_keys=True, ensure_ascii=False)
        if key not in self._lexical_cache:
            rows = self._vectorstore._collection.get(where=where, include=["documents", "metadatas"], limit=5001)
            documents = [Document(page_content=text, metadata=metadata)
                         for text, metadata in zip(rows.get("documents") or [], rows.get("metadatas") or [])
                         if text and metadata]
            self._lexical_cache[key] = (
                build_bm25_retriever(documents, k=top_k) if 0 < len(documents) <= 5000 else None
            )
        retriever = self._lexical_cache[key]
        if retriever is None:
            return []
        retriever.k = top_k
        tokens = set(simple_tokens(question))
        # 토큰이 하나도 겹치지 않는 문단을 BM25의 동점 결과로 추가하지 않는다.
        return [doc for doc in retriever.invoke(question)
                if tokens.intersection(simple_tokens(doc.page_content))]

    def search(self, question: str, top_k: int = 5) -> list[dict]:
        """첫 검색은 벡터, 이후 검색은 주석 우선 벡터·BM25 결합으로 새 근거를 찾는다."""
        if not question.strip() or not 1 <= top_k <= 10:
            raise ValueError("검색 질문과 1~10 범위의 top_k가 필요합니다.")
        if not (self.persist_directory / "chroma.sqlite3").is_file():
            raise FileNotFoundError("선택한 버전의 XML Chroma 색인이 없습니다. XML 파이프라인으로 먼저 색인하세요.")
        from fin_research.retrieval.chroma_retriever import create_chroma
        from fin_research.retrieval.xml_retrieval import result_from_document
        if self._vectorstore is None:
            self._vectorstore = create_chroma(self.persist_directory, self.collection_name)
        if self._vectorstore._collection.count() == 0:
            raise ValueError("선택한 XML 컬렉션에 청크가 없습니다.")
        retry = self._calls > 0
        section = self.section
        if retry and not section:
            section = "연결재무제표 주석" if self.target.statement_scope == "consolidated" else "재무제표 주석"
        where = self._filter(section)
        count = min(top_k * 2, 20) if retry else top_k
        hits = self._vectorstore.similarity_search_with_score(question, k=count, filter=where)
        if retry and not hits and not self.section:
            where = self._filter()
            hits = self._vectorstore.similarity_search_with_score(question, k=count, filter=where)
        ranked = [[(document, float(distance)) for document, distance in hits]]
        if retry:
            ranked.append([(document, None) for document in self._lexical_hits(question, where, count)])
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
                scores[identifier] = scores.get(identifier, 0) + 1 / (60 + rank)
                candidates.setdefault(identifier, result)
        identifiers = sorted(candidates, key=lambda key: (retry and key in self._seen, -scores[key], key))[:top_k]
        results = [{**candidates[key], "rank": rank, "pdf_page": None, "evidence_id": f"S{rank}",
                    **({"rrf_score": scores[key]} if retry else {})}
                   for rank, key in enumerate(identifiers, 1)]
        self._seen.update(identifiers)
        self._calls += 1
        self.last_search_info = {"strategy": "hybrid_notes" if retry else "vector",
                                 "collection": self.collection_name, "chunk_version": self.chunk_version,
                                 "persist_directory": str(self.persist_directory), "filter": where,
                                 "candidate_count": len(candidates)}
        return results
