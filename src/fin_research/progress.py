"""최종 JSON과 분리하여 실행 진행 상황을 즉시 표시한다."""

import sys
from collections.abc import Callable
from datetime import datetime
from time import monotonic


ProgressCallback = Callable[[str], None]


def report_progress(callback: ProgressCallback | None, message: str) -> None:
    """라이브러리 호출에서는 콜백을 지정할 때만 진행 정보를 전달한다."""
    if callback is not None:
        callback(message)


class TerminalProgress:
    """경과 시간과 함께 stderr에 출력하고 각 단계에서 즉시 flush한다."""

    def __init__(self):
        self.started_at = monotonic()

    def __call__(self, message: str) -> None:
        elapsed = monotonic() - self.started_at
        # 외부 문서의 줄바꿈·제어문자로 터미널 진행 표시가 깨지지 않도록 정리한다.
        cleaned = " ".join("".join(
            character if character.isprintable() else " " for character in message
        ).split())
        print(
            f"[{datetime.now():%H:%M:%S} | +{elapsed:.1f}초] {cleaned}",
            file=sys.stderr, flush=True,
        )
