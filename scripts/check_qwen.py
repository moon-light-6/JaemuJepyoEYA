"""Qwen 서버의 모델 목록과 JSON 주장 추출을 DB 없이 확인한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fin_research.llm.client import get_chat_model
from fin_research.llm.output_parser import invoke_structured
from fin_research.verify.state import ClaimExtraction


def check_models(model) -> None:
    """설정된 Qwen 서버에 해당 모델이 실제로 올라와 있는지 검사한다."""
    # API 키와 원문 응답은 출력하지 않는다. OpenAI로 자동 우회하지 않는다.
    key = model.openai_api_key.get_secret_value()
    request = Request(str(model.openai_api_base).rstrip("/") + "/models",
                      headers={"Authorization": f"Bearer {key}"})
    with urlopen(request, timeout=10) as response:
        payload = json.loads(response.read(2_000_000).decode("utf-8"))
    names = {row.get("id") for row in payload.get("data", []) if isinstance(row, dict)}
    if model.model_name not in names:
        raise ValueError("서버 모델명이 설정과 다릅니다. --served-model-name과 models.yaml의 model을 맞추세요.")


def check_extraction(model) -> ClaimExtraction:
    """실제 검증과 같은 JSON 파서를 사용해 한국어 숫자 주장을 한 개 추출한다."""
    sample = "서진시스템의 2025년 연결 매출액은 123,456원이다."
    result = invoke_structured(
        model,
        "다음 가상 테스트 문장에서 숫자 주장을 정확히 하나 추출하세요. "
        "text는 원문 그대로, item은 매출액, value는 원 단위 숫자, unit은 원, "
        "scope는 consolidated, period는 current, page는 1입니다.\n" + sample,
        ClaimExtraction,
    )
    if len(result.claims) != 1:
        raise ValueError("JSON은 생성됐지만 테스트 주장을 정확히 하나 추출하지 못했습니다.")
    claim = result.claims[0]
    if (claim.text, claim.item, claim.value, claim.unit, claim.year, claim.scope) != (
        sample, "매출액", 123456, "원", 2025, "consolidated"
    ):
        raise ValueError("JSON 형식은 맞지만 테스트 문장의 항목·금액·단위·연도·기준이 다릅니다.")
    return result


def main() -> int:
    """연결 확인 후 선택적으로 한국어 JSON 추출까지 시험한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="모델 목록뿐 아니라 한국어 JSON 추출도 확인")
    args = parser.parse_args()
    try:
        model = get_chat_model("qwen")
        check_models(model)
        print(f"Qwen 서버 연결 성공: {model.model_name}")
        if args.smoke:
            result = check_extraction(model)
            print("한국어 JSON 주장 추출 성공")
            print(result.model_dump_json(indent=2))
    except HTTPError as exc:
        print(f"Qwen 서버 HTTP 오류: {exc.code}. 서버 주소·인증·실행 로그를 확인하세요.", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, OSError):
        print("Qwen 서버에 연결하지 못했습니다. GPU PC의 서버와 QWEN_BASE_URL을 확인하세요.", file=sys.stderr)
        return 1
    except Exception as exc:
        # 모델/파서 오류를 실패로 보고하되 인증 정보가 섞인 원문 오류를 그대로 노출하지 않는다.
        print(f"Qwen 확인 실패 ({type(exc).__name__}). 모델명·JSON 출력·서버 로그를 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
