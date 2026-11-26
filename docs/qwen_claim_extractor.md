# Qwen3.5-4B 주장 추출 전환

이번 전환은 `claim_extractor`만 대상으로 한다. DB 조회·금액 비교·집계는 기존 코드다.
다른 조사·작성 역할은 GPT 설정을 유지한다. Qwen 연결 실패 시 GPT로 자동 전환하지 않는다.

## 변경 위치

- `configs/models.yaml`: Qwen 서버 설정 추가, 기존 GPT 역할 지정은 주석으로 보존.
- `llm/client.py`: `QWEN_BASE_URL` 환경변수로 GPU PC 주소를 지정하도록 연결.
- `verify/nodes/claim_extract.py`: 모델 선택과 Qwen 전용 입력 크기·동시 호출 수 적용.
- `configs/budget.yaml`: Qwen은 3천 글자 단위 묶음, 동시 추출 1개로 시작. 한 페이지 자체가 길면 중간 분할하지 않는 기존 정책을 유지한다.
- `scripts/demo_verify.py`: `--claim-model qwen|small` 비교 실행, 결과에 `model_info` 기록.
- `scripts/check_qwen.py`: DB 없이 서버 모델명·한국어 JSON 추출 확인.
- `scripts/serve_qwen.sh`: 별도 GPU PC의 Linux/WSL2용 서버 실행 파일.

API 키나 기존 `.env`, `.env.example` 파일은 수정하지 않았다. 서버 실행 환경은 프로젝트의 Windows 가상환경과 분리한다.

## 1. GPU PC 준비

RTX 5060 Ti 16GB PC에서 진행한다. 현재 노트북의 내장 그래픽에서 vLLM GPU 서버를 실행하는 절차가 아니다.
GPU PC의 Linux 또는 WSL2 Ubuntu에서 `nvidia-smi`로 GPU가 보이는지 먼저 확인한다.
동시에 실행 중인 GPU 작업을 종료한다. RAM·VRAM·드라이버·vLLM의 호환성은 설치한 환경에서 확인한다.

최초 설치는 Linux/WSL2 터미널에서 진행한다. `uv`가 설치되어 있다는 전제다.

```bash
uv venv --python 3.12 ~/.venvs/qwen35-server
source ~/.venvs/qwen35-server/bin/activate
uv pip install vllm --torch-backend=auto --extra-index-url https://wheels.vllm.ai/nightly
```

Qwen 공식 모델 카드의 vLLM 설치 지침을 기준으로 작성했다. 최초 성공 후 `uv pip freeze`로 버전을 기록한다.
이 프로젝트의 `requirements.txt`에 vLLM을 추가하지 않는다. 처음 실행하면 모델 파일 다운로드가 발생한다.

프로젝트를 GPU PC에도 준비한 뒤, 각자 저장소를 둔 경로로 이동한다.
아래 `REPO_DIR`는 예시이므로 본인의 실제 Linux/WSL 경로로 바꾼다.
프로젝트 루트는 `scripts/`, `configs/`, `src/` 폴더가 있는 위치다.

```bash
REPO_DIR='/본인/저장소/경로/JaemuJepyoEYA'
cd "$REPO_DIR"
```

Windows 폴더를 WSL에서 사용하는 경우 `C:\`는 `/mnt/c/`, `D:\`는 `/mnt/d/`로 대응한다.
예를 들어 `C:\work\JaemuJepyoEYA`에 저장했다면 WSL 경로는 `/mnt/c/work/JaemuJepyoEYA`다.
Linux/WSL 내부에 복제했다면 해당 저장소의 실제 Linux 경로를 사용한다.

프로젝트 루트로 이동한 상태에서 서버를 실행한다.

```bash
bash scripts/serve_qwen.sh
```

서버가 준비됐다는 로그를 확인한 뒤 다음 단계로 간다. 16GB에서의 실제 모델 적재·속도·정확도는 GPU PC에서 검증해야 한다.
8,192 문맥 길이는 입력과 출력을 합친 한도다. 문맥 길이 초과 시 PDF 묶음 크기를 줄이거나, 메모리를 확인한 뒤 서버 문맥 길이를 조정한다.

## 2. 프로젝트에서 연결 확인

먼저 같은 GPU PC의 Windows 프로젝트 터미널에서 연결하는 구성을 권장한다.
이하 Python 명령어도 각자의 프로젝트 루트에서 실행한다.
CMD에서 다음을 실행한다. 셸 변수는 현재 터미널에만 적용한다.

```bat
set QWEN_BASE_URL=http://127.0.0.1:8000/v1
python -X utf8 scripts/check_qwen.py --smoke
```

PowerShell에서는 `set` 대신 아래 문법을 쓴다.

```powershell
$env:QWEN_BASE_URL = 'http://127.0.0.1:8000/v1'
python -X utf8 scripts/check_qwen.py --smoke
```

다른 노트북에서 사용할 때 `localhost`는 GPU PC를 가리키지 않는다.
SSH 포트 전달을 이미 사용할 수 있다면 로컬 8000 포트를 GPU PC의 서버 포트로 연결해 같은 주소를 사용할 수 있다.
직접 LAN으로 연결할 때는 서버의 바인딩 주소·WSL 네트워크·방화벽과 API 인증을 별도로 구성하고, `QWEN_BASE_URL`을 GPU PC 주소로 지정한다.
이 작업에서 포트 개방·접근 권한 변경은 수행하지 않았다. 인증된 서버에는 `QWEN_API_KEY`를 지정한다.
로컬 서버가 인증을 요구하지 않으면 프로젝트는 더미 키를 사용하며, 기존 `OPENAI_API_KEY`를 Qwen 키로 복사할 필요가 없다.

## 3. 합성 PDF 실행

CMD 한 줄 명령이다. `--claim-model qwen`을 생략해도 현재 기본 역할은 Qwen이다.

```bat
python -X utf8 scripts/demo_verify.py --pdf "output\pdf\서진시스템_검증용_합성보고서.pdf" --company "서진시스템" --claim-model qwen --json "data\runs\서진시스템_Qwen_검증결과.json"
```

기존 GPT 모델 비교 실행은 다음 명령으로 보존했다. OpenAI API 키와 사용료가 필요하다.

```bat
python -X utf8 scripts/demo_verify.py --pdf "output\pdf\서진시스템_검증용_합성보고서.pdf" --company "서진시스템" --claim-model small --json "data\runs\서진시스템_GPT_검증결과.json"
```

주장 10개, 항목·금액·단위·연도·기준·쪽 번호, 일치 5개/불일치 5개를 각각 비교한다.
판정 개수만 같아도 추출 내용이 다르면 성공으로 인정하지 않는다. 사용 토큰·재시도·실행 시간도 함께 기록한다.
Qwen JSON 오류는 기존 Pydantic 파서가 확인하며 재시도 횟수 내에서도 실패하면 해당 묶음의 오류로 남긴다.

## 4. GPT 기본값으로 되돌리기

한 번 비교할 때는 `--claim-model small`만 지정하면 된다.
전체 기본값을 되돌릴 때는 `models.yaml`의 주석으로 보존한 GPT `claim_extractor` 줄을 활성화하고 Qwen 역할 줄을 주석 처리한다.
설정 캐시가 있으므로 변경 후 Python 프로세스를 새로 실행한다.

## 확인 범위와 자료

프로젝트 연결 코드는 모의 HTTP 응답·가짜 DB·기존 회귀 테스트로 확인한다. 이는 Qwen 가중치를 실제 GPU에 적재한 시험과 구분한다.
GPU 서버에서 `check_qwen.py --smoke`와 PDF 명령이 성공한 후 실제 전환 검증이 완료된다.

- Qwen 공식 모델 카드: https://huggingface.co/Qwen/Qwen3.5-4B
- vLLM Qwen 실행 안내: https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3.5.html
- vLLM GPU/WSL 안내: https://docs.vllm.ai/en/stable/getting_started/installation/gpu/

루프의 Planner/Evaluator/Findings가 사용하는 `json_schema` 엄격 출력 방식은 아직 전환하지 않았다.
주장 추출은 기존 프롬프트 JSON 출력과 파서 방식으로 동작하며, 현재 설정은 미세 조정이나 학습을 수행하지 않는다.
