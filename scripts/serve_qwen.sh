#!/usr/bin/env bash
# Qwen3.5-4B 공개 모델을 GPU PC의 Linux/WSL2 전용 환경에서 실행한다.
# 기존 Windows 프로젝트 .venv에 vLLM을 설치하거나 GPT 실행 파일을 덮어쓰지 않는다.
set -euo pipefail

# 최초 접속은 같은 PC의 localhost로 제한한다. 다른 PC 연결은 문서의 접속 방법을 따른다.
# 16GB VRAM의 시작 설정이며, 다른 GPU 작업을 종료한 뒤 실제 메모리를 확인한다.
exec vllm serve Qwen/Qwen3.5-4B \
  --served-model-name Qwen/Qwen3.5-4B \
  --host "${QWEN_HOST:-127.0.0.1}" \
  --port "${QWEN_PORT:-8000}" \
  --language-model-only \
  --reasoning-parser qwen3 \
  --max-model-len "${QWEN_MAX_MODEL_LEN:-8192}" \
  --max-num-seqs 1 \
  --gpu-memory-utilization "${QWEN_GPU_MEMORY_UTILIZATION:-0.85}"
