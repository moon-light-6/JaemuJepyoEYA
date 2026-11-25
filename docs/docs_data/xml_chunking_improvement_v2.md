# XML 청킹 개선(ver.2)과 검색 평가 실행 가이드 

이 문서는 삼성전자 2025년 연결 사업보고서를 예시로, 원본 수집부터 정답지 준비, XML 청킹 v2, 검색 평가와 기존 결과 비교까지 순서대로 설명한다. 다른 기업에서는 회사명, 기업코드, 사업연도, 접수번호와 연결·별도 기준을 바꿔 사용한다.

## 시작하기 전에


청킹과 정답지 생성에 앞서 **원본 ZIP이 있는지 확인하고, 없으면 `collect.py`로 받는다.** 삼성전자 예시는 다음과 같다.

```zsh
python scripts/ingestion/collect.py --corp-code 00126380 --fiscal-year 2025
```

기존에 ZIP이 있다면 다시 수집할 필요는 없다. 다른 기업에서는 수집된 접수번호가 앞으로 사용할 정답지와 같은지 확인한다.

## 1. 전체 과정 한눈에 보기

```text
collect.py
  └─ 사업보고서 원본 ZIP
       ↓
정답지 상태 확인
  ├─ 기존 정답지가 없으면 기존 draft 생성 → 사람이 검증
  ├─ 기존 정답지는 있는데 빠진 문항이 있으면 추가 draft 생성 → 사람이 검증
  └─ 기존 18문항이 이미 검증됐으면 그대로 사용
       ↓
run_pipeline_xml_v2.py
  ├─ ZIP에서 XML 문서 생성 또는 기존 XML 문서 사용
  ├─ build_xml_chunks_v2.py로 청크 생성
  ├─ v2 청크를 별도 Chroma에 색인
  └─ 검증된 정답지로 검색 평가 → v2 결과 저장
       ↓
같은 정답지를 사용한 기존 결과와 비교
```

**서진님이 작업한 추가 문항 생성과 청킹 v2는 서로 다른 작업**이다. 추가 문항 생성은 평가에 쓸 질문과 정답을 보완한다. 청킹 v2는 검색기가 찾아볼 XML 조각을 개선한다.

## 2. 기존 청킹과 개선된 청킹 

### 기존 방식

`run_pipeline_xml.py`는 재무제표와 주석을 기본 **600자·겹침 100자**로 나눈다. 청크에는 해당 부분의 본문과 XML 출처 정보가 담긴다.

줄바꿈을 고려해 나누지만 긴 표 행이 잘릴 수 있다. 또한 뒤쪽 청크에는 표 제목, 금액 단위, 연도 정보가 빠질 수 있다.

### 개선 방식(v2)

`run_pipeline_xml_v2.py`는 재무제표 본표를 **행 단위로** 나누고, 각 청크 앞에 회사·연도·연결/별도·표 제목과 인식된 단위·열 정보를 붙인다. 주석은 기존 방식으로 나눈다.

삼성전자 재무상태표 청크의 예시는 다음과 같다.

```text
[삼성전자 | 2025년 연결 | 재무상태표 | 단위: 백만원 | 열: 과 목, 주 석, 제57기(2025년), 제56기(2024년)]
7. 재고자산 | 8 | 52,636,828 | 51,754,865
```

기본 크기는 **600자·겹침 100자**다. 한 행이 크기를 넘으면 행 전체를 보존한다. 단위와 열 정보는 표에서 인식한 내용을 사용하므로 실제 공시와 대조한다. XML 출처 정보를 유지하고 별도 Chroma 저장소에 색인해 기존 검색기로 평가한다.

## 3. 파일 이름에서 꼭 구분할 것

```text
data/processed/xml/dart/00126380/2025/consolidated/
  20260310002820_xml_documents.json          공통 XML 문서
  20260310002820_xml_chunks.json             기존 방식 청크
  20260310002820_xml_chunks_v2.json          개선 방식 청크

evaluation/dataset/xml/00126380/2025/consolidated/
  20260310002820_retrieval.json              기존 질문·정답
  20260310002820_retrieval_v2.json           기존에 없던 추가 질문·정답

evaluation/results/xml/00126380/2025/consolidated/
  20260310002820_retrieval.json              기존 청크의 평가 결과
  20260310002820_retrieval_v2.json           v2 청크의 평가 결과
```


## 4. 정답지가 없거나 문항이 부족할 때

### 빠른 실행: 기업명만 입력

이제 `run_pipeline_xml_v2.py`는 기업명(또는 종목코드), 사업연도, 연결/별도만으로도 실행할 수 있다.
OpenDART 고유번호 파일에서 `corp_code`를 찾고, 사업보고서 목록에서 접수번호를 고른 뒤
새 기업도 기업명 또는 DART 식별자로 실행할 수 있으며 `configs/companies.yaml`에 추가하지 않는다. 동명 기업이 여러 개이면 대표자, 업종 중분류와 고유번호를 확인한 뒤 후보 번호를 입력한다. draft 생성 후 문항을 검증하고 `verified`로 바꾼 뒤 평가한다.
이미 있는 기업 설정은 자동으로 변경하지 않는다.
정정본이 여러 개면 기본으로 최신 접수본을 사용한다. 필요한 XML 원본 ZIP도 없으면 같은 실행에서
다운로드한 뒤 XML 문서 생성·청킹·색인·평가까지 이어 간다.

```zsh
python scripts/pipelines/run_pipeline_xml_v2.py \
  --company "카페24" \
  --fiscal-year 2025 \
  --statement-scope consolidated \
  --make-draft-dataset
```

설정 파일을 변경하지 않고 조회·실행만 하려면 `--no-sync-config`를 붙인다. 접수번호를 고정하려면
`--rcept-no 202603...`를 함께 지정한다.

### 4-1. 기존 정답지가 없는 기업

v2 파이프라인의 `--make-draft-dataset`은 XML 원본 ZIP이 없으면 먼저 자동 수집한 뒤 정답지 후보와 v2 청크를 만든다. 다음은 명령 형식을 보여 주는 삼성전자 예시다. **현재 삼성전자는 기존 정답지가 있어 실행하면 해당 평가셋을 덮어쓰지 않는다.**

```zsh
python scripts/pipelines/run_pipeline_xml_v2.py \
  --company "삼성전자" \
  --corp-code 00126380 \
  --fiscal-year 2025 \
  --rcept-no 20260310002820 \
  --statement-scope consolidated \
  --make-draft-dataset
```

👉 다른 기업에서는 입력값을 해당 기업에 맞게 바꾼다. 

기존 정답지가 없는 경우 `dataset/..._retrieval.json`에 draft 문항이 생성된다. 이 옵션은 재무제표 본표에서 단위를 우선 찾으며, 자동 생성은 최대 18문항을 보장하지 않는다. OpenDART 계정의 접수번호가 요청한 공시와 다르면 draft 생성을 중단한다.

### 4-2. 기존 정답지는 있지만 문항이 부족한 기업

서진님이 작업한 `--make-draft-dataset-v2`로 기존 정답지에 **없는 질문**을 추가 후보로 만든다. 

기존 정답지가 먼저 있어야 하며,이 옵션은 기존 파이프라인과 청킹 v2 파이프라인에서 모두 사용할 수 있다.

다음은 카페24 형식의 예시다. 삼성전자는 이미 18문항이 검증되어 있으므로 이 명령은 추가 후보가 없다고 출력하고 파일을 만들지 않는다. 


```zsh
python scripts/pipelines/run_pipeline_xml_v2.py \
  --corp-code 00260879 \
  --fiscal-year 2025 \
  --rcept-no 20260317000899 \
  --statement-scope consolidated \
  --make-draft-dataset-v2
```
👉 **실제 추가 작업은 문항이 부족한 기업의 값으로 바꾸어 실행한다.**


추가 후보가 있으면 `evaluation/dataset/xml/.../접수번호_retrieval_v2.json`에 **추가 문항만** 저장한다. 기존 질문 전체를 복사하는 파일이 아니다. 파일이 이미 있으면 덮어쓰지 않는다.


### 4-3. draft를 직접 검증하기

생성된 정답지 JSON을 열어 각 질문에 대해 다음을 원본 공시와 대조한다.

1. 회사, 공시 접수번호, 사업연도와 연결·별도 기준이 맞는지 확인한다.
2. 계정명과 정답 금액이 실제로 같은 행과 해당 연도 열에 있는지 확인한다.
3. 단위와 음수 부호를 확인한다.
4. `gold_sources`의 원본 파일과 `source_xpath`가 확인한 표를 가리키는지 확인한다.
5. 확인한 문항만 `"annotation_status": "verified"`로 바꿔 저장한다.


## 5. 청킹 v2 평가와 기존 결과 비교

**비교할 때는 기존 청킹과 v2 청킹에 같은 검증 문항을 사용한다.** 정답지를 확인한 뒤 아래 두 경우 중 해당하는 순서로 진행한다.

### 5-1. 기존 정답지의 18문항이 모두 검증된 경우

삼성전자는 기존 정답지의 18문항이 모두 `verified`이고, 그 18문항으로 평가한 기존 결과도 있다. 따라서 추가 정답지를 만들지 않고 `--baseline-path`를 지정해 바로 비교할 수 있다.

```zsh
python scripts/pipelines/run_pipeline_xml_v2.py \
  --corp-code 00126380 \
  --fiscal-year 2025 \
  --rcept-no 20260310002820 \
  --statement-scope consolidated \
  --baseline-path evaluation/results/xml/00126380/2025/consolidated/20260310002820_retrieval.json
```

명령은 기존 18문항을 읽고 v2 청크 생성 → Chroma 색인 → 검색 평가를 진행한다. 결과는 `results/.../20260310002820_retrieval_v2.json`에 저장되고, 터미널 맨 아래에 기존 결과와 `위치+값@5` 비교가 출력된다. `검증된 평가 문항: 18개`도 확인한다.

이 비교는 지정한 baseline이 **현재와 같은 질문과 정답으로 평가된 결과**일 때 의미가 있다. 같은 18문항이라고 숫자만 같아서는 안 된다. 추가 정답지가 나중에 생기면 v2 파이프라인은 검증된 추가 문항을 자동으로 합치므로, 추가 문항까지 포함한 baseline을 다시 만든다.

### 5-2. 기존 정답지에 빠진 문항을 추가해 비교하는 경우

#### 👉 **4-2**에서 추가 문항을 만들고 해당 문항들에 대한  검증을 4-3에서 마쳤다면, **기존 청킹으로 baseline 평가 → v2 청킹 평가와 비교** 순서로 진행한다. 


아래는 카페24 예시다.

현재 카페24 파일은 기존 13문항과 추가 3문항이 모두 `verified`인 상태다. 이 상태라면 아래 평가에는 총 16문항이 사용된다. 작성 또는 검증 상태가 바뀌었다면 실제 출력의 문항 수를 확인한다.

**1. 기존 청킹으로 같은 문항을 먼저 평가**

기존 파이프라인에 `--include-v2`를 붙여 기존 정답지와 추가 정답지의 `verified` 문항을 함께 평가한다.

```zsh
python scripts/pipelines/run_pipeline_xml.py \
  --company "카페24" \
  --corp-code 00260879 \
  --fiscal-year 2025 \
  --rcept-no 20260317000899 \
  --statement-scope consolidated \
  --include-v2
```

이 결과가 새 baseline인 `results/.../20260317000899_retrieval.json`에 저장된다. 기존 파일을 덮어쓰므로 이전 baseline을 보관하려면 실행 전에 별도 이름으로 복사한다. 터미널에서 `v1 13개 + v2 3개`처럼 실제 평가 문항 수를 확인한다.

**2. v2 청킹을 평가하고 baseline과 비교**

```zsh
python scripts/pipelines/run_pipeline_xml_v2.py \
  --corp-code 00260879 \
  --fiscal-year 2025 \
  --rcept-no 20260317000899 \
  --statement-scope consolidated \
  --baseline-path evaluation/results/xml/00260879/2025/consolidated/20260317000899_retrieval.json
```

v2 파이프라인은 추가 정답지가 있으면 기존 문항과 검증된 추가 문항을 자동으로 합친다. 터미널에 `검증된 평가 문항: 16개`가 출력되는지 확인한다. 결과는 `results/.../20260317000899_retrieval_v2.json`에 저장되고, 맨 아래에 1단계 baseline과의 `위치+값@5` 비교가 나온다.

추가 파일에 `verified` 문항이 없으면 기존 문항만 사용한다. 두 정답지에 같은 문항 ID가 있으면 평가를 중단한다. baseline과 v2의 질문이나 정답 출처가 다르면 자동 비교를 건너뛰므로 문항을 확인하고 양쪽을 같은 조건으로 다시 평가한다.

## 6. 검색 평가 결과 읽기

각 검색기마다 6개 지표가 출력된다.

- `위치@3`, `위치@5`는 상위 3개 또는 5개 안에 정답으로 지정된 XML 파일과 XPath의 청크가 나왔는지 보여 준다.
- `값@3`, `값@5`는 상위 결과에 정답 값이 나왔는지 보여 준다.
- `위치+값@3`, `위치+값@5`는 **같은 청크에 정답 위치와 정답 값이 함께 있는지** 보여 준다.

예를 들어 Chroma의 `위치+값@5`가 83.3%이고 평가 문항이 18개면 15문항에서 상위 5개 안에 정답 위치와 금액이 함께 있는 청크를 찾았다는 뜻이다.

현재 저장된 삼성전자 결과는 같은 기존 18문항에 대해 다음과 같다.

| 검색기 | 기존 청킹 위치+값@5 | 청킹 v2 위치+값@5 |
|---|---:|---:|
| BM25 | 11.1% | 22.2% |
| Chroma | 44.4% | 83.3% |
| MMR | 38.9% | 44.4% |
| Hybrid-RRF | 44.4% | 72.2% |

baseline 비교는 질문 ID, 질문 내용과 정답 출처가 같은지 확인한다. 기존 결과 JSON에 정답 금액은 기록돼 있지 않아 두 실행의 금액이 같은지는 결과 파일만으로 자동 확인하지 못한다. 정답지 변경 이력과 실제 금액을 함께 확인한다.


## ※ 참고) 정답지 문항 수 확인하기

필요할 때 다음 명령으로 정답지의 전체 문항 수와 `draft`·`verified` 상태를 확인할 수 있다. 예시는 삼성전자이며, 다른 기업은 파일 경로를 바꿔 실행한다.


기업별로 맨 위 네 줄만 바꾸면 기존 정답지와 추가 정답지를 합쳐 확인할 수 있다. 
```zsh
CORP_CODE=00126380
YEAR=2025
SCOPE=consolidated
RCEPT_NO=20260310002820

python - "$CORP_CODE" "$YEAR" "$SCOPE" "$RCEPT_NO" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

corp, year, scope, receipt = sys.argv[1:]
folder = Path("evaluation/dataset/xml") / corp / year / scope
items = []

for suffix in ("retrieval.json", "retrieval_v2.json"):
    path = folder / f"{receipt}_{suffix}"
    if path.exists():
        items.extend(json.loads(path.read_text(encoding="utf-8")))
    else:
        print(f"파일 없음: {path.name}")

print("전체 문항:", len(items))
print("검증 상태:", dict(Counter(item.get("annotation_status") for item in items)))
PY
```

#### 실행결과
```zsh
전체 문항: 18
검증 상태: {'verified': 18}
```


기업명으로 실행할 때 같은 이름의 기업이 여러 개면 후보의 대표자와 업종 중분류를 확인하고 후보 번호를 선택한다. OpenDART는 업종명을 직접 반환하지 않으므로 업종코드에 대응하는 중분류명을 표시하며, 해당 분류를 찾지 못하면 확인 불가로 표시한다. 비대화형 실행에서는 `--corp-code`를 지정한다.
