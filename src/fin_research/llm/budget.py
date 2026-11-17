"""실행(run) 1회분의 LLM 사용량 집계와 상한 관리.

    budget = Budget.from_config()                     # budget.yaml의 limits로 시작
    llm = get_llm_for_role("writer", budget=budget)   # 이 LLM의 호출은 자동 집계
    budget.summary()                                  # Run Store·평가용 집계

- Budget은 실행마다 새로 만들고, 그 실행의 모든 노드가 같은 객체를 공유한다.
  (LangGraph state에는 넣지 않는다: Lock이 있어 직렬화되지 않음 → config로 전달)
- BudgetCallbackHandler가 호출 "전"에 상한을 확인하고, 호출 "후"에 토큰을 기록한다.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from fin_research.llm.client import PROJECT_ROOT

BUDGET_CONFIG_PATH = PROJECT_ROOT / "configs" / "budget.yaml"


class BudgetExceeded(RuntimeError):
    """예산 상한 초과. 그래프 실행을 중단시키는 용도."""


@lru_cache(maxsize=1)
def load_budget_config(path: Path = BUDGET_CONFIG_PATH) -> dict:
    """budget.yaml 전체 (limits + research). supervisor는 research 값을 여기서 읽는다."""
    if not path.is_file():
        raise FileNotFoundError(f"예산 설정 파일이 없습니다: {path}")
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict) or "limits" not in config:
        raise ValueError(f"budget.yaml에 'limits' 섹션이 없습니다: {path}")
    return config


def _new_role_usage() -> dict[str, int]:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0}


class Budget:
    """상한값 + 누적 사용량. 병렬 노드에서 동시에 기록해도 안전하다(Lock)."""

    def __init__(
        self,
        max_llm_calls: int | None = None,
        max_total_tokens: int | None = None,
        max_seconds: float | None = None,
    ):
        self.max_llm_calls = max_llm_calls
        self.max_total_tokens = max_total_tokens
        self.max_seconds = max_seconds

        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.by_role: defaultdict[str, dict[str, int]] = defaultdict(_new_role_usage)

        self._started = time.monotonic()  # 시스템 시계 변경에 영향받지 않는 경과 시간
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, path: Path = BUDGET_CONFIG_PATH) -> Budget:
        """budget.yaml의 limits로 생성 (null = 제한 없음)."""
        limits = load_budget_config(path)["limits"] or {}
        return cls(
            max_llm_calls=limits.get("max_llm_calls"),
            max_total_tokens=limits.get("max_total_tokens"),
            max_seconds=limits.get("max_seconds"),
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def elapsed_sec(self) -> float:
        return time.monotonic() - self._started

    def record(self, role: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """LLM 호출 1회분 기록."""
        with self._lock:
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            r = self.by_role[role]
            r["calls"] += 1
            r["input_tokens"] += input_tokens
            r["output_tokens"] += output_tokens

    def check(self) -> None:
        """상한을 이미 다 썼으면 BudgetExceeded. 다음 호출을 하기 '전'에 부른다."""
        with self._lock:
            calls, tokens = self.calls, self.total_tokens
        elapsed = self.elapsed_sec

        if self.max_llm_calls is not None and calls >= self.max_llm_calls:
            raise BudgetExceeded(f"LLM 호출 상한 초과: {calls:,} / {self.max_llm_calls:,}회")
        if self.max_total_tokens is not None and tokens >= self.max_total_tokens:
            raise BudgetExceeded(f"토큰 상한 초과: {tokens:,} / {self.max_total_tokens:,}")
        if self.max_seconds is not None and elapsed >= self.max_seconds:
            raise BudgetExceeded(f"시간 상한 초과: {elapsed:,.1f} / {self.max_seconds:,}초")

    def summary(self) -> dict[str, Any]:
        """현재까지 사용량 (Run Store 저장·평가 지표·UI 표시용)."""
        with self._lock:
            return {
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "elapsed_sec": round(self.elapsed_sec, 2),
                "by_role": {role: dict(u) for role, u in self.by_role.items()},
                "limits": {
                    "max_llm_calls": self.max_llm_calls,
                    "max_total_tokens": self.max_total_tokens,
                    "max_seconds": self.max_seconds,
                },
            }


class BudgetCallbackHandler(BaseCallbackHandler):
    """LLM에 붙여서 호출마다 Budget을 확인·기록하는 LangChain 콜백."""

    # LangChain은 기본적으로 콜백 예외를 경고만 남기고 삼킨다.
    # True여야 BudgetExceeded가 실제로 호출을 막는다.
    raise_error = True

    def __init__(self, budget: Budget, role: str):
        self.budget = budget
        self.role = role

    def on_chat_model_start(self, serialized: dict[str, Any], messages: Any, **kwargs: Any) -> None:
        # 호출 "전": 이미 상한에 도달했으면 여기서 중단 (돈·시간을 쓰기 전에)
        self.budget.check()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        # 호출 "후": 토큰 기록. usage_metadata가 없으면(일부 vLLM/가짜 모델) 호출 수만 센다.
        usage = None
        try:
            usage = response.generations[0][0].message.usage_metadata
        except (IndexError, AttributeError):
            pass
        usage = usage or {}
        self.budget.record(
            self.role,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )


if __name__ == "__main__":
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    budget = Budget(max_llm_calls=2)
    fake = FakeMessagesListChatModel(
        responses=[AIMessage("a", usage_metadata=usage)] * 3,
        callbacks=[BudgetCallbackHandler(budget, "writer")],
    )

    fake.invoke("1")
    fake.invoke("2")
    print(budget.summary())  # calls 2, input 200, output 100

    try:
        fake.invoke("3")  # 3번째 → 호출 전에 막혀야 함
    except BudgetExceeded as e:
        print("OK:", e)

    print(Budget.from_config().summary()["limits"])
    print(load_budget_config()["research"])
