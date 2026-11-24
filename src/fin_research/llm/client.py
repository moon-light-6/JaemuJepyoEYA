from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

PROJECT_ROOT =Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "configs" / "models.yaml"
load_dotenv(PROJECT_ROOT / ".env")

# get_chat_model(name) name= small, large
@lru_cache(maxsize=1)
def load_model_config(path: Path = CONFIG_PATH) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"모델 설정 파일이 없습니다: {path}")
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict) or "models" not in config:
        raise ValueError(f"models.yaml에 'models' 섹션이 없습니다: {path}")

    return config


def _resolve_api_key(name: str, cfg: dict) -> str:
    """cfg에서 api_key_env를 꺼내(cfg에서 제거됨) 실제 키 값을 반환한다.

    GPT에는 OpenAI 키가 필요하다. 별도 모델 서버는 설정된 키를 쓰고, 없으면 더미 값을 쓴다.
    """
    key_env = cfg.pop("api_key_env", None)
    api_key = os.getenv(key_env) if key_env else None
    if not api_key:
        if cfg.get("base_url") is None:
            raise RuntimeError(
                f"'{name}' 모델의 API 키가 없습니다. .env에 {key_env}를 설정하세요."
            )
        # 인증 없는 로컬 모델 서버용 기본값이다. 인증된 서버는 api_key_env의 키가 필요하다.
        api_key = "EMPTY"
    return api_key


def _resolve_base_url(cfg: dict) -> None:
    """Qwen 서버 주소를 환경변수로 덮어쓰고 잘못된 주소는 호출 전에 거부한다."""
    from urllib.parse import urlparse

    # 환경변수 이름은 클라이언트 생성자에 넘길 수 없으므로 설정에서 꺼낸다.
    variable = cfg.pop("base_url_env", None)
    if not variable:
        return
    value = os.getenv(variable)
    if value is not None:
        if not value.strip():
            raise ValueError(f"{variable}가 비어 있습니다. Qwen 서버의 /v1 주소를 지정하세요.")
        cfg["base_url"] = value.strip().rstrip("/")
    parsed = urlparse(str(cfg.get("base_url") or ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError(f"{variable}에는 올바른 HTTP(S) 서버 주소를 지정하세요.")
    if parsed.path.rstrip("/") != "/v1":
        raise ValueError(f"{variable} 주소는 /v1로 끝나야 합니다.")


def get_chat_model(name: str, **overrides) -> ChatOpenAI:

    # ① 모델 설정 찾기
    models = load_model_config()["models"]
    if name not in models:
        raise ValueError(f"알 수 없는 모델 이름: {name!r} (사용 가능: {list(models)})")
    if name == "embedding":
        raise ValueError("'embedding'은 채팅 모델이 아닙니다. get_embeddings()를 사용하세요.")

    # ② 캐시 원본을 건드리지 않도록 새 dict로 합치기 (뒤의 overrides가 우선)
    cfg = {**models[name], **overrides}

    # Qwen 모델은 QWEN_BASE_URL 또는 명시한 기본 서버로만 연결한다.
    # 기존 GPT 모델은 base_url_env가 없으므로 기존 설정을 그대로 사용한다.
    _resolve_base_url(cfg)

    # ③ API 키: 환경변수 이름을 꺼내고(cfg에서 제거) 실제 값을 읽기
    api_key = _resolve_api_key(name, cfg)

    # ④ 값이 None인 항목은 넘기지 않음 (base_url: null → OpenAI 기본 주소)
    cfg = {k: v for k, v in cfg.items() if v is not None}

    # ⑤ yaml 키 이름 = ChatOpenAI 인자 이름이라 그대로 펼쳐서 넘김
    return ChatOpenAI(api_key=api_key, **cfg)


def get_embeddings(name: str = "embedding", **overrides) -> OpenAIEmbeddings:
    # ① 모델 설정 찾기
    models = load_model_config()["models"]
    if name not in models:
        raise ValueError(f"알 수 없는 모델 이름: {name!r} (사용 가능: {list(models)})")

    # ② 새 dict로 합치기
    cfg = {**models[name], **overrides}

    # ③ API 키
    api_key = _resolve_api_key(name, cfg)

    # ④ vLLM 등 OpenAI가 아닌 서버면 tiktoken 토큰화 없이 원문 텍스트를 보냄
    if cfg.get("base_url") is not None:
        cfg.setdefault("check_embedding_ctx_length", False)

    # ⑤ None 제거 후 생성
    cfg = {k: v for k, v in cfg.items() if v is not None}
    return OpenAIEmbeddings(api_key=api_key, **cfg)


if __name__ == "__main__":
    llm = get_chat_model("small")
    print(llm.model_name, llm.temperature)  # gpt-4.1-mini 0.0
    print(llm.invoke("삼성전자는 어떤 회사야? 한 문장으로").content)

    print(get_chat_model("large", temperature=0).temperature)  # 0
    print(get_chat_model("large").temperature)  # 0.2 ← 캐시 오염 없음

    try:
        get_chat_model("medium")
    except ValueError as e:
        print("OK:", e)

    emb = get_embeddings()
    vec = emb.embed_query("영업이익률")
    print(len(vec))  # 1536 (text-embedding-3-small)
    print(vec[:3])
