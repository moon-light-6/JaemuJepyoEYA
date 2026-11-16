"""기업·사업연도·보고서를 식별하고 검색 데이터 위치를 관리한다."""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SearchTarget:
    """하나의 기업·연도·재무제표 기준·공시 보고서를 나타낸다."""

    corp_code: str
    fiscal_year: int
    statement_scope: str
    rcept_no: str

    def __post_init__(self):
        if not re.fullmatch(r"\d{8}", self.corp_code):
            raise ValueError("corp_code는 8자리 숫자 문자열이어야 합니다.")

        if type(self.fiscal_year) is not int or self.fiscal_year < 1900:
            raise ValueError("fiscal_year가 올바르지 않습니다.")

        if self.statement_scope not in {"consolidated", "separate"}:
            raise ValueError(
                "statement_scope는 consolidated 또는 separate여야 합니다."
            )

        if not re.fullmatch(r"\d{14}", self.rcept_no):
            raise ValueError("rcept_no는 14자리 숫자 문자열이어야 합니다.")

    def chunks_path(self, project_root: str | Path) -> Path:
        """기존 데이터 처리 파이프라인의 청크 JSON 경로를 반환한다."""

        return (
            Path(project_root)
            / "data"
            / "processed"
            / "dart"
            / self.corp_code
            / str(self.fiscal_year)
            / f"{self.rcept_no}_chunks.json"
        )

    def collection_name(self) -> str:
        """기업과 보고서별 Chroma 컬렉션 이름을 생성한다."""

        return (
            f"fin_{self.corp_code}_{self.fiscal_year}_"
            f"{self.statement_scope}_{self.rcept_no}_kure_v1"
        )

    def source_file(self) -> str:
        """원본 PDF 파일명을 반환한다."""

        return f"{self.rcept_no}.pdf"
