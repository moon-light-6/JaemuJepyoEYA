"""모델 라우터: 역할(노드) 이름 → models.yaml의 roles 설정에 맞는 LLM.

노드는 get_llm_for_role("writer")처럼 역할만 말하고, 어떤 모델을 쓸지는 models.yaml이 정한다.
모든 노드가 LLM을 얻는 단일 통로이므로, 이후 budget 콜백도 여기서 붙인다.
(research/loop/router.py의 '도구 라우터'와는 다른 모듈)
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from fin_research.llm.budget import Budget, BudgetCallbackHandler
from fin_research.llm.client import get_chat_model, load_model_config

EMBEDDING_KEY = "embedding"


def _roles() -> dict:
    return load_model_config().get("roles") or {}


def list_roles() -> list[str]:
    """models.yaml에 등록된 역할 이름 목록."""
    return list(_roles())


def validate_roles() -> None:
    """모든 역할이 존재하는 채팅 모델을 가리키는지 실행 전에 한 번에 검사한다."""
    models = load_model_config()["models"]
    errors = []
    for role, cfg in _roles().items():
        model_key = (cfg or {}).get("model")
        if model_key is None:
            errors.append(f"{role}: 'model'이 지정되지 않음")
        elif model_key not in models:
            errors.append(f"{role}: 없는 모델 {model_key!r}")
        elif model_key == EMBEDDING_KEY:
            errors.append(f"{role}: 임베딩 모델은 채팅 역할에 쓸 수 없음")
    if errors:
        raise ValueError(
            "models.yaml roles 설정 오류:\n  - " + "\n  - ".join(errors)
            + f"\n(사용 가능한 모델: {list(models)})"
        )


def get_llm_for_role(
    role: str,
    budget: Budget | None = None,
    model_key: str | None = None,
    **overrides,
) -> ChatOpenAI:
    """역할 이름으로 LLM을 만든다.

    우선순위(뒤가 이김): models의 모델 기본값 → roles의 역할 값 → 호출 시 overrides
    budget을 주면 이 LLM의 호출이 그 Budget에 집계되고, 상한 초과 시 BudgetExceeded.
    model_key('large'|'small')를 주면 이번 호출만 역할의 모델 선택을 바꾼다 (CLI·실험용).
    """
    # ① roles 가져오기
    roles = _roles()

    # ② 역할 찾기
    if role not in roles:
        raise ValueError(f"알 수 없는 역할: {role!r} (사용 가능: {list(roles)})")

    # ③ 캐시 원본을 건드리지 않도록 복사한 뒤 model 키 분리
    role_cfg = dict(roles[role] or {})
    role_model_key = role_cfg.pop("model", None)
    model_key = model_key or role_model_key
    if model_key is None:
        raise ValueError(f"역할 {role!r}에 'model'이 지정되지 않았습니다 (models.yaml roles).")

    # ④ 예산 콜백 붙이기 (ChatOpenAI 생성자의 callbacks 인자로 전달)
    if budget is not None:
        overrides = {**overrides, "callbacks": [BudgetCallbackHandler(budget, role)]}

    # ⑤ 역할 값 + 호출 overrides를 합쳐 client에 넘김
    return get_chat_model(model_key, **{**role_cfg, **overrides})


if __name__ == "__main__":
    validate_roles()
    print("리스트:",list_roles())

    w = get_llm_for_role("writer")
    print(w.model_name, w.temperature)  # gpt-4.1 0.3

    r = get_llm_for_role("loop_router")
    print(r.model_name, r.temperature)  # gpt-4.1-mini 0.0

    print(get_llm_for_role("writer", temperature=0).temperature)  # 0
    print(get_llm_for_role("writer").temperature)  # 0.3 ← 캐시 오염 없음

    try:
        get_llm_for_role("wirter")  # 오타
    except ValueError as e:
        print("OK:", e)
