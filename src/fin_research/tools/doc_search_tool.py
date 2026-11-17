"""조사 담당자가 기존 KURE-v1·Chroma 검색기를 사용할 수 있도록 연결합니다."""

from pathlib import Path


class DocumentSearchTool:
    """한 번의 조사에서 임베딩 모델과 검색 저장소를 재사용합니다."""

    def __init__(self, persist_directory: Path):
        self.persist_directory = persist_directory
        self._vectorstore = None

    def search(self, question: str, top_k: int = 5) -> list[dict]:
        if not question.strip():
            raise ValueError("검색 질문이 비어 있습니다.")
        if not 1 <= top_k <= 10:
            raise ValueError("top_k는 1~10 사이여야 합니다.")
        if not (self.persist_directory / "chroma.sqlite3").is_file():
            raise FileNotFoundError("Chroma 저장소가 없습니다. scripts/build_chroma.py를 먼저 실행하세요.")

        # 명령어 도움말 조회 시에는 임베딩 모델을 불러오지 않습니다.
        from fin_research.retrieval.chroma_retriever import create_chroma, search_chroma

        if self._vectorstore is None:
            self._vectorstore = create_chroma(self.persist_directory)
        results = search_chroma(
            self.persist_directory, question, k=top_k, vectorstore=self._vectorstore,
        )
        return [
            {**result, "evidence_id": f"S{index}"}
            for index, result in enumerate(results, start=1)
        ]


def search_documents(question: str, persist_directory: Path, top_k: int = 5) -> list[dict]:
    """기존 색인을 사용해 검색하고, 각 청크에 출처 ID를 붙여 반환합니다."""
    return DocumentSearchTool(persist_directory).search(question, top_k)
