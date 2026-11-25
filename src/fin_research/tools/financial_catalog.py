"""별칭 YAML을 조사 지표·표 종류·표시 이름의 단일 기준으로 사용한다."""

import yaml

from fin_research.verify.accounts import ALIASES_PATH, load_aliases


def load_catalog(path=ALIASES_PATH):
    """모든 별칭을 조사 지표로 연결하고 잘못된 재무제표 범위는 거부한다."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["items"]
    catalog = {}
    for key, alias in load_aliases(path).items():
        metric = "operating_profit" if key == "operating_income" else key
        statements = tuple(raw[key].get("statements", ()))
        if not statements or not set(statements) <= {"IS", "CIS", "BS", "CF"}:
            raise ValueError(f"{key}: 조회할 statements를 YAML에 지정하세요.")
        catalog[metric] = {"label": alias.label, "ids": alias.account_ids,
                           "names": alias.names, "statements": statements}
    return catalog


CATALOG = load_catalog()


def capabilities():
    """Brief와 Planner가 같은 조회·계산 지원 목록을 확인하도록 안내문을 만든다."""
    return {"metrics": {key: item["label"] for key, item in CATALOG.items()},
            "ratios": {"debt_ratio": "부채비율", "current_ratio": "유동비율",
                       "roe": "평균 전체자본 ROE", "operating_margin": "영업이익률"},
            "limits": "단일 기업·단일 사업연도·선택한 연결/별도·동일 공시의 원화 연간 실적. 업계 비교·부문별 비율·임의 계산 미지원."}
