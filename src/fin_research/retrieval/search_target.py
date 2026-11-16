"""검색 대상 기업·사업연도·연결 기준·공시번호 관리."""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SearchTarget:
    """하나의 공시 보고서에 대한 검색 대상 정보."""

    corp_code: str
    fiscal_year: int
    statement_scope: str
    rcept_no: str

    def __post_init__(self):
        """검색 대상 필드의 형식을 검사한다."""

        if (
            not isinstance(self.corp_code, str)
            or not re.fullmatch(r"\d{8}", self.corp_code)
        ):
            raise ValueError("corp_code는 8자리 숫자 문자열이어야 합니다.")

        if type(self.fiscal_year) is not int or self.fiscal_year <= 0:
            raise ValueError("fiscal_year는 양의 정수여야 합니다.")

        if self.statement_scope not in ("consolidated", "separate"):
            raise ValueError(
                "statement_scope는 consolidated 또는 separate여야 합니다."
            )

        if (
            not isinstance(self.rcept_no, str)
            or not re.fullmatch(r"\d{14}", self.rcept_no)
        ):
            raise ValueError("rcept_no는 14자리 숫자 문자열이어야 합니다.")

    @property
    def collection_name(self) -> str:
        """검색 대상에 해당하는 Chroma 컬렉션 이름."""

        return (
            f"fin_{self.corp_code}_{self.fiscal_year}_"
            f"{self.statement_scope}_{self.rcept_no}_kure_v1"
        )

    def chunks_path(self, project_root: str | Path) -> Path:
        """청크 JSON 경로."""

        return (
            Path(project_root)
            / "data"
            / "processed"
            / "dart"
            / self.corp_code
            / str(self.fiscal_year)
            / f"{self.rcept_no}_chunks.json"
        )
