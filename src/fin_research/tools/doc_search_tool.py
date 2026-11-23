"""조사 담당자가 기존 KURE-v1·Chroma 검색기를 사용할 수 있도록 연결합니다."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fin_research.retrieval.search_target import SearchTarget


class DocumentSearchTool:
    """한 번의 조사에서 임베딩 모델과 검색 저장소를 재사용합니다.

    target(SearchTarget)을 주면 그 기업·연도·공시의 컬렉션을 검색합니다
    (scripts/build_chroma.py가 만드는 기업별 컬렉션). 주지 않으면 기존 단일 컬렉션을 씁니다.
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
                "scripts/build_chroma.py를 먼저 실행하세요."
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
    """검수·평가한 XML 전용 컬렉션을 검색하고 XPath 출처를 유지한다."""

    def __init__(self, persist_directory: Path, target: SearchTarget, *, section: str | None = None):
        """공시별 컬렉션과 선택적 섹션 필터를 지정한다."""
        super().__init__(persist_directory, target)
        self.section = section

    @property
    def collection_name(self) -> str:
        """PDF와 섞이지 않는 XML 컬렉션 이름을 반환한다."""
        return self.target.xml_collection_name

    def search(self, question: str, top_k: int = 5) -> list[dict]:
        """연도·연결/별도·XML 형식으로 필터하고 기업·접수번호도 검사한다."""
        if not question.strip() or not 1 <= top_k <= 10:
            raise ValueError("검색 질문과 1~10 범위의 top_k가 필요합니다.")
        if not (self.persist_directory / "chroma.sqlite3").is_file():
            raise FileNotFoundError("XML Chroma 색인이 없습니다. XML 파이프라인으로 먼저 색인하세요.")
        from fin_research.retrieval.chroma_retriever import create_chroma
        from fin_research.retrieval.xml_retrieval import result_from_document

        if self._vectorstore is None:
            self._vectorstore = create_chroma(self.persist_directory, self.collection_name)
        if self._vectorstore._collection.count() == 0:
            raise ValueError("선택한 XML 컬렉션에 청크가 없습니다.")
        conditions = [{"source_format": "xml"}, {"fiscal_year": self.target.fiscal_year},
                      {"statement_scope": self.target.statement_scope}]
        if self.section:
            conditions.append({"section": self.section})
        hits = self._vectorstore.similarity_search_with_score(question, k=top_k, filter={"$and": conditions})
        results = []
        prefix = f"{self.target.corp_code}_{self.target.fiscal_year}_{self.target.statement_scope}_xml_"
        for index, (document, distance) in enumerate(hits, 1):
            result = result_from_document(document, index, distance=float(distance))
            if not result["chunk_id"].startswith(prefix) or not result["source_file"].startswith(self.target.rcept_no):
                raise ValueError("XML 검색 결과의 기업 또는 접수번호가 다릅니다.")
            results.append({**result, "pdf_page": None, "evidence_id": f"S{index}"})
        return results
