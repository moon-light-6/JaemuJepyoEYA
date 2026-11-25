"""Qwen 모델 라우팅·HTTP 요청·GPT 보존·검증 CLI의 연결을 확인한다."""

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import APIConnectionError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from fin_research.llm.client import get_chat_model, load_model_config
from fin_research.llm.output_parser import invoke_structured
from fin_research.llm.router import get_llm_for_role, validate_roles
from fin_research.verify.nodes.claim_extract import extraction_limits
from fin_research.verify.state import ClaimExtraction
from scripts import check_qwen
from scripts.demo import demo_verify


class QwenTransitionTests(unittest.TestCase):
    """실제 GPU나 외부 API 없이 공개 모델 전환의 요청 경계를 검증한다."""

    def test_claim_role_changes_to_qwen_and_gpt_remains_selectable(self):
        """기본 주장 추출만 Qwen이고 기존 GPT와 나머지 역할은 남아 있는지 확인한다."""
        config = load_model_config()
        self.assertEqual(config["roles"]["claim_extractor"]["model"], "qwen")
        self.assertEqual(config["roles"]["loop_evaluator"]["model"], "small")
        self.assertEqual(config["models"]["large"]["model"], "gpt-4.1")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-gpt-key"}, clear=True):
            validate_roles()
            model = get_llm_for_role("claim_extractor", model_key="small")
        self.assertEqual(model.model_name, "gpt-4.1-mini")
        self.assertIsNone(model.openai_api_base)

    def test_qwen_uses_own_endpoint_and_needs_no_openai_key(self):
        """Qwen 주소·키만 사용하고 환경변수용 키를 모델 요청에 잘못 넘기지 않는다."""
        with patch.dict(os.environ, {"QWEN_BASE_URL": "http://gpu-pc:8000/v1/", "QWEN_API_KEY": "test-qwen-key"}, clear=True):
            model = get_llm_for_role("claim_extractor")
        self.assertEqual(str(model.openai_api_base), "http://gpu-pc:8000/v1")
        self.assertEqual(model.openai_api_key.get_secret_value(), "test-qwen-key")
        self.assertEqual(model.extra_body, {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertNotIn("base_url_env", model.model_kwargs)
        self.assertEqual(load_model_config()["models"]["qwen"]["base_url"], "http://127.0.0.1:8000/v1")

    def test_empty_or_malformed_qwen_address_is_rejected(self):
        """잘못된 주소를 사용하거나 OpenAI 기본 주소로 조용히 우회하지 못하게 한다."""
        for address in ("", "ftp://gpu/v1", "http://gpu:8000", "http://gpu/v1?key=test"):
            with self.subTest(address=address), patch.dict(os.environ, {"QWEN_BASE_URL": address}, clear=True):
                with self.assertRaises(ValueError):
                    get_chat_model("qwen")

    def test_qwen_http_response_passes_actual_claim_parser(self):
        """OpenAI 호환 HTTP 응답을 실제 SDK와 파서가 처리하고 사고 모드를 끄는지 확인한다."""
        requests = []
        def respond(request):
            """Qwen 서버 응답 형식을 대체하고 실제 전송된 요청을 수집한다."""
            requests.append(request)
            return httpx.Response(200, json={
                "id": "test-qwen-response", "object": "chat.completion", "created": 0,
                "model": "Qwen/Qwen3.5-4B",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps({"claims": [{
                        "text": "매출액은 123원이다.", "item": "매출액", "value": 123,
                        "unit": "원", "year": 2025, "scope": "consolidated", "page": 1,
                    }]}, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
            })
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with patch.dict(os.environ, {"QWEN_BASE_URL": "http://gpu-pc:8000/v1"}, clear=True):
                model = get_chat_model("qwen", http_client=client)
                result = invoke_structured(model, "매출액은 123원이다.", ClaimExtraction)
        self.assertEqual(result.claims[0].value, 123)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "gpu-pc")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["model"], "Qwen/Qwen3.5-4B")
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("base_url_env", payload)

    def test_qwen_connection_failure_does_not_fall_back_to_gpt(self):
        """연결이 끊겨도 GPT로 재요청하거나 비용을 발생시키는 우회를 하지 않는다."""
        requests = []
        def fail(request):
            """GPU 서버가 꺼진 상황을 네트워크 없이 재현한다."""
            requests.append(request)
            raise httpx.ConnectError("test offline", request=request)
        with httpx.Client(transport=httpx.MockTransport(fail)) as client:
            with patch.dict(os.environ, {"QWEN_BASE_URL": "http://gpu-pc:8000/v1"}, clear=True):
                model = get_chat_model("qwen", http_client=client)
                with self.assertRaises(APIConnectionError):
                    model.invoke("테스트")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "gpu-pc")

    def test_qwen_and_gpt_keep_separate_extraction_limits(self):
        """Qwen만 작은 묶음·단일 호출로 실행하고 기존 GPT 처리량 설정을 보존한다."""
        settings = {"batch_max_chars": 6000, "max_concurrent_extract": 4}
        self.assertEqual(extraction_limits(SimpleNamespace(model_name="Qwen/Qwen3.5-4B"), settings), (3000, 1))
        self.assertEqual(extraction_limits(SimpleNamespace(model_name="gpt-4.1-mini"), settings), (6000, 4))
        with self.assertRaises(ValueError):
            extraction_limits(SimpleNamespace(model_name="Qwen/Qwen3.5-4B"), {**settings, "qwen_max_concurrent_extract": 0})

    def test_model_probe_rejects_another_model(self):
        """서버 연결만 성공하고 다른 모델이 올라온 경우를 전환 성공으로 보지 않는다."""
        model = SimpleNamespace(model_name="Qwen/Qwen3.5-4B", openai_api_base="http://gpu-pc:8000/v1",
                                openai_api_key=SimpleNamespace(get_secret_value=lambda: "test-key"))
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"data":[{"id":"other-model"}]}'
        with patch.object(check_qwen, "urlopen", return_value=response):
            with self.assertRaises(ValueError):
                check_qwen.check_models(model)

    def test_demo_model_selection_and_json_metadata(self):
        """기본 Qwen과 명시한 GPT가 그래프 설정에 전달되고 결과 파일에 기록되는지 확인한다."""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "test.pdf"
            source.touch()
            for key, options in (("qwen", []), ("small", ["--claim-model", "small"])):
                output = Path(folder) / key / "result.json"
                graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"claims": [], "verdicts": [], "errors": []}))
                args = ["demo_verify.py", "--pdf", str(source), "--company", "서진시스템", "--json", str(output), *options]
                with patch.object(sys, "argv", args), patch.object(demo_verify, "build_graph", return_value=graph):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(asyncio.run(demo_verify.main()), 0)
                config = graph.ainvoke.call_args.args[1]
                self.assertEqual(config["configurable"]["claim_model_key"], key)
                self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["model_info"]["key"], key)


if __name__ == "__main__":
    unittest.main()
