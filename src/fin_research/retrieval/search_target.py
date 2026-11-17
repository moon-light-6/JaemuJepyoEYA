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

    def scope_dir(self, project_root: str | Path) -> Path:
        """연결/별도 기준별 검색 산출물을 모아 둘 폴더."""

        # 원본 PDF·ZIP은 연결/별도 공통 자료다. 반면 pages·chunks·평가 결과는
        # 연결/별도에 따라 달라지므로 scope까지 폴더로 분리한다.
        # 접수번호는 이 폴더 바로 아래의 파일명으로 사용한다.
        return (
            Path(project_root)
            / "data" / "processed" / "dart"
            / self.corp_code
            / str(self.fiscal_year)
            / self.statement_scope
        )

    def pages_path(self, project_root: str | Path) -> Path:
        """페이지 범위를 정리한 검색용 원문 경로."""

        return self.scope_dir(project_root) / f"{self.rcept_no}_pages.json"

    def chunks_path(self, project_root: str | Path) -> Path:
        """페이지 원문을 청킹한 검색 인덱스 입력 경로."""

        return self.scope_dir(project_root) / f"{self.rcept_no}_chunks.json"

    def validation_path(self, project_root: str | Path) -> Path:
        """청킹 구조 점검 결과 경로."""

        return self.scope_dir(project_root) / f"{self.rcept_no}_chunks_validation.json"

    def pipeline_path(self, project_root: str | Path) -> Path:
        """자동 파이프라인이 판단한 범위·단위 등의 실행 기록 경로."""

        # 파이프라인 기록은 scope 폴더 안에서 접수번호 자체를 파일명으로 사용한다.
        return self.scope_dir(project_root) / f"{self.rcept_no}.json"

    def dataset_path(self, project_root: str | Path) -> Path:
        """접수번호별 평가 문항을 저장할 경로."""

        return (
            Path(project_root) / "evaluation" / "dataset"
            / self.corp_code
            / str(self.fiscal_year)
            / self.statement_scope
            / f"{self.rcept_no}.json"
        )

    def result_path(self, project_root: str | Path) -> Path:
        """접수번호별 검색 성능 평가 결과 경로."""

        return (
            Path(project_root) / "evaluation" / "results"
            / self.corp_code
            / str(self.fiscal_year)
            / self.statement_scope
            / f"{self.rcept_no}.json"
        )
