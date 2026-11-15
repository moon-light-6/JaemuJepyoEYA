# 검색기 구현 및 성능 평가

> 공식 PDF 청킹 이후 BM25·Chroma·MMR·Hybrid-RRF 검색기를 만들고, 검색 성능을 검증하는 과정을 다룬다.

이 문서는 `docs/chunking_document.md`에서 준비한 검색용 청크와 평가셋을 입력으로 사용한다. 현재는 **BM25, Chroma, MMR·Hybrid-RRF 검색기 구현과 검색 성능 평가** 까지 기록했다. 검색 실패 원인 분석과 개선은 후속 단계에서 진행한다.

현재 MVP 검색 대상은 삼성전자 2025년 사업보고서의 **연결재무제표·연결 주석(PDF 물리 페이지 83~195)** 이다.

## 입력과 구현 파일

| 파일 | 역할 |
| --- | --- |
| `data/processed/dart/00126380/2025/20260310002820_chunks.json` | 검색 대상 청크 294개와 원문 출처 메타데이터 |
| `evaluation/dataset/eval_retrieval.json` | 검색 성능 평가용 질문·정답·Gold page 20개(10번에서 사용) |
| `src/fin_research/retrieval/bm25_retriever.py` | 청크 로드, 토큰화, BM25 생성 및 검색 함수 |
| `src/fin_research/retrieval/__init__.py` | 검색 모듈 패키지 파일 |
| `scripts/search_bm25.py` | CLI에서 질문을 입력하고 상위 청크를 확인하는 실행 스크립트 |
| `tests/test_bm25_retriever.py` | BM25 단위 테스트 9개 |
| `src/fin_research/retrieval/chroma_retriever.py` | KURE-v1 로드, Chroma 색인·재사용·유사도 검색 |
| `scripts/build_chroma.py` | 청크 임베딩 및 ChromaDB 최초 색인 실행 |
| `scripts/search_chroma.py` | Chroma Top-K 유사도 검색 CLI |
| `tests/test_chroma_retriever.py` | 가짜 임베딩·메모리 Chroma 기반 단위 테스트 9개 |
| `data/chroma_samsung/` | 로컬 Chroma 영구 저장소(Git 커밋 대상 아님) |
| `src/fin_research/retrieval/hybrid_retriever.py` | BM25와 Chroma 검색 결과를 `chunk_id` 기준으로 RRF 병합 |
| `scripts/search_mmr.py` | Chroma MMR 검색 실행 CLI |
| `scripts/search_hybrid.py` | BM25 + Chroma Hybrid-RRF 검색 실행 CLI |
| `tests/test_mmr_retriever.py` | MMR 옵션·결과·입력 검증 단위 테스트 7개 |
| `tests/test_hybrid_retriever.py` | RRF 점수·중복·통합 검색 단위 테스트 9개 |
| `evaluation/retrieval_metrics.py` | 평가셋 로드, 출처 일치 판정, Hit@K 집계 |
| `scripts/evaluate_retrieval.py` | 네 가지 검색 방식의 20문항 자동 평가·결과 저장 |
| `tests/test_retrieval_metrics.py` | 평가 지표 계산 단위 테스트 7개 |
| `tests/test_evaluate_retrieval.py` | 평가 실행 흐름·JSON 저장 단위 테스트 5개 |
| `evaluation/results/retrieval_baseline.json` | 개선 전 기준선: 검색 방식별 성능·문항별 결과 |

`pdf_page`는 PDF 내부에 인쇄된 번호가 아니라 실제 PDF 파일의 **물리 페이지 번호**다. 검색 결과의 출처를 비교할 때는 `source_file`과 `pdf_page`를 함께 확인한다.

## 1. BM25 키워드 검색기

BM25는 질문에 포함된 단어와 문서 청크의 단어 빈도·희소성·문서 길이를 이용해 관련 문서를 정렬하는 검색 방식이다. 임베딩 모델이나 LLM API 호출 없이 로컬 청크 텍스트만으로 검색할 수 있다.

수업의 `2_Retriever.ipynb`에서 사용한 `BM25Retriever`와 `simple_tokens()` 방식을 기반으로 구현했다.

### 1-1. 실행 환경

프로젝트 루트에서 실행한다. 기존 `requirements.txt`에 다음 의존성을 추가했다.

```text
langchain-community
rank-bm25
```

```powershell
python -m pip install -r requirements.txt
```

### 1-2. 청크 JSON 로드 및 Document 변환

`load_chunk_documents()`는 `*_chunks.json` 배열을 읽어 각 청크를 LangChain `Document`로 변환한다.

- `page_content`: 검색 대상인 청크의 `text`
- `metadata`: `chunk_id`, `company`, `fiscal_year`, `statement_scope`, `source_file`, `pdf_page`, `section` 등 원본 필드
- 필수 필드가 누락되거나 텍스트가 비어 있으면 오류 처리

메타데이터를 유지하므로 검색한 청크가 어떤 PDF 페이지에서 왔는지 추적할 수 있다.

### 1-3. 토큰화 및 BM25 검색기 생성

수업에서 사용한 단순 정규식 토큰화를 그대로 기준선으로 사용했다.

예를 들어 `매출액`과 `매출액은`은 서로 다른 토큰이므로 한국어 조사로 인해 검색 성능이 낮아질 수 있다. 현재는 수업의 기본 방식을 유지하고, 토큰화 개선은 11번에서 실험한다.

### 1-4. 검색 결과 반환

`search_bm25()`는 질문을 `retriever.invoke(question)`에 전달하고 관련성이 높은 청크를 반환한다. 각 결과에는 순위, 청크 ID, 본문 텍스트, 원본 PDF 파일명·페이지와 메타데이터를 포함한다.

현재 LangChain `BM25Retriever.invoke()`의 반환값에는 BM25 원점수가 포함되지 않아 별도 `score` 필드는 제공하지 않는다.

### 1-5. 실행 명령

```powershell
python scripts/search_bm25.py --question "2025년 연결 기준 매출액은 얼마인가?" --top-k 5
```

정상 실행 시 검색 질문, 검색 대상 청크 수, 상위 5개 청크의 순위·PDF 페이지·`chunk_id`·본문 일부를 출력한다. 실행 당시 검색 대상은 **294개 청크**였다.

## 2. BM25 실행 결과 및 확인된 한계

위 질문의 수동 실행 결과 상위 5개 검색 페이지는 다음과 같았다.

| 순위 | PDF 물리 페이지 | 검색 결과의 주요 내용 |
| ---: | ---: | --- |
| 1 | 125 | 관계기업·공동기업 투자 현황 |
| 2 | 129 | 유형자산 관련 설명·법적 조치 |
| 3 | 149 | 기타자본항목·이익잉여금 |
| 4 | 129 | 소송 관련 내용 |
| 5 | 149 | 배당 관련 내용 |

평가 가이드의 매출액 질문에 지정된 Gold page는 **85쪽**이다. 따라서 이 **한 질문에 대해서는 Hit@3 및 Hit@5 모두 실패**했다.

이는 BM25 프로그램이 실행되지 않았다는 뜻이 아니라, 검색기가 현재 질문에 적절한 근거 페이지를 상위 5개 결과로 찾지 못했다는 뜻이다. 원인은 아직 확정하지 않았다. 11번에서 한국어 토큰화, 표 텍스트 추출, 청크 분리, 중복 페이지 및 순위를 비교·개선한다.

20개 평가 질문 전체의 Hit@3·Hit@5는 아직 계산하지 않았다.

## 3. 단위 테스트 및 회귀 테스트

`tests/test_bm25_retriever.py`에서 실제 PDF 대신 임시 가상 청크를 사용해 다음 항목을 검증했다.

- 토큰화 결과
- JSON을 LangChain Document로 변환하는지
- 청크 ID·PDF 페이지·연결 기준 등의 메타데이터 보존
- 관련 청크가 검색되는지
- 설정한 Top-K 개수가 적용되는지
- 빈 질문 및 잘못된 `k` 처리
- 필수 필드 누락 및 빈 청크 JSON 처리

BM25 단위 테스트만 실행:

```powershell
python -m unittest discover -s tests -p "test_bm25_retriever.py" -v
```

전체 회귀 테스트:

```powershell
python -m unittest discover -s tests -v
```

**로컬 실행 확인 결과**

```text
Ran 27 tests in 2.306s

OK
```

전체 27개 테스트가 통과했다. 이 결과는 프로그램의 동작과 예외 처리에 대한 검증이며, 검색 품질이 충분하다는 뜻은 아니다.

## 현재 BM25 결과

- 검색 대상: 삼성전자 2025 사업보고서의 PDF 83~195쪽
- 입력 청크: 294개
- 검색 방식: LangChain `BM25Retriever` + 정규식 토큰화
- 기본 반환 개수: Top-5
- 검색 결과: 청크 본문, `chunk_id`, `source_file`, `pdf_page`, 메타데이터
- BM25 단위 테스트: 9개 통과
- 전체 회귀 테스트: 27개 통과
- 알려진 검색 실패: 매출액 질문의 Gold page 85쪽이 Top-5에 없음
- 정량 검색 평가: 10번에서 20개 질문으로 진행 예정

## 4. Chroma 벡터 검색기

Chroma는 청크 텍스트를 임베딩 벡터로 저장하고 질문과 의미적으로 가까운 청크를 검색한다. 수업의 `2_Retriever.ipynb`에서 다룬 Chroma 유사도 검색·로컬 저장소 재사용 방식을 바탕으로 구현했다. **7번 BM25와 같은 294개 청크**를 사용한다.

### 4-1. 임베딩 모델과 실행 환경

설계 문서에서 정한 임베딩 모델 `nlpai-lab/KURE-v1`을 사용한다. 한국어 사업보고서 본문·주석의 의미 기반 검색을 위한 우선 모델이며, BM25를 보완할 가능성이 있다. 실제 우수성은 10번의 검색 성능 평가에서 확인한다.

기존 `requirements.txt`에 다음 의존성을 추가했다.

```text
chromadb
langchain-chroma
langchain-huggingface
sentence-transformers
```

프로젝트 루트에서 설치한다.

```powershell
python -m pip install -r requirements.txt
```

`create_embedding_model()`은 `torch.cuda.is_available()`로 실행 장치를 자동 선택한다. CUDA를 사용할 수 있으면 GPU, 아니면 CPU를 사용한다. GPU를 사용하려면 해당 GPU에 호환되는 CUDA 지원 PyTorch 환경이 필요하다.

```python
device = "cuda" if torch.cuda.is_available() else "cpu"

embedding_model = HuggingFaceEmbeddings(
    model_name="nlpai-lab/KURE-v1",
    model_kwargs={"device": device},
    encode_kwargs={"normalize_embeddings": True},
)
```

임베딩 모델은 최초 실행 시 다운로드될 수 있다. GPU 자동 선택 코드는 구현했지만, **RTX 5060 등 다른 팀원 GPU에서의 동작은 별도 확인이 필요하다**.

### 4-2. 최초 색인 및 기존 데이터 재사용

`scripts/build_chroma.py`는 기존 `load_chunk_documents()`로 JSON 청크를 읽고, `chroma_retriever.py`의 `build_chroma()`를 호출한다.

- 컬렉션 이름: `samsung_2025_consolidated_kure_v1`
- 저장 경로: `data/chroma_samsung/`
- 청크 ID: 기존 JSON의 `chunk_id`를 Chroma 문서 ID로 사용
- 배치 크기: 32개
- 메타데이터: Chroma가 지원하는 기본 자료형(`str`, `int`, `float`, `bool`)만 유지
- 최초 색인 시 문서 저장; 재실행 시 문서 수와 ID가 같으면 기존 컬렉션 재사용

```powershell
python scripts/build_chroma.py
```

**첫 실행 결과**

```text
최초 색인 시작: 294개 청크
색인 진행: 32/294
...
색인 진행: 294/294
최초 색인 완료
저장된 청크 수: 294
색인 작업 종료: 총 294개 청크
```

**동일 명령 재실행 결과**

```text
기존 Chroma 데이터 재사용
저장된 청크 수: 294
색인 작업 종료: 총 294개 청크
```

재실행 후 문서 수가 294개로 유지되어 중복 적재 없이 기존 데이터를 사용하는 것을 확인했다.

### 4-3. 저장된 Chroma 검색

`search_chroma()`는 기존 컬렉션을 열어 사용자 질문을 임베딩하고 `similarity_search_with_score()`로 유사한 청크를 찾는다. 기본 반환 개수는 Top-5다.

반환 필드는 `rank`, `chunk_id`, `text`, `source_file`, `pdf_page`, `metadata`, `distance`다. BM25와 동일한 출처 필드를 제공하므로 이후 Hybrid-RRF와 검색 성능 평가에서 결과를 연결할 수 있다.

```powershell
python scripts/search_chroma.py --question "2025년 연결 기준 매출액은 얼마인가?" --top-k 5
```

질문 임베딩은 실행 시 계산하지만, 저장된 **294개 문서의 임베딩을 매 검색마다 다시 생성하지는 않는다**.

### 4-4. 수동 검색 결과

위 질문의 실제 Top-5 결과는 다음과 같다.

| 순위 | PDF 물리 페이지 | 거리 | 검색 내용 |
| ---: | ---: | ---: | --- |
| 1 | 85 | 0.6500 | 연결 손익계산서의 매출액 |
| 2 | 86 | 0.8352 | 연결 포괄손익계산서 |
| 3 | 89 | 0.8585 | 연결 현금흐름표 |
| 4 | 146 | 0.9020 | 약정 관련 설명 |
| 5 | 87 | 0.9128 | 연결 자본변동표 |

이 질문의 Gold page는 **PDF 물리 페이지 85쪽**이다. 따라서 이 **한 질문에 한해** Chroma는 정답 페이지를 1위로 검색하여 Hit@3·Hit@5에 모두 성공했다. 같은 질문에 대한 BM25 결과는 두 지표에서 모두 실패했다.

1위 청크에서 `매출액 (주30) 333,605,938 ...` 텍스트를 확인했다. 금액의 열·기간 관계는 수치 검증 시 원본 표와 대조해야 한다.

`distance`는 같은 컬렉션에서 일반적으로 작을수록 가깝다는 의미이며, 검색 정확도나 확률이 아니다. 검색기 간 원점수도 직접 비교하지 않는다. **전체 20문항에 대한 성능 우열은 아직 평가하지 않았다.**

### 4-5. Chroma 단위 테스트 및 회귀 테스트

`tests/test_chroma_retriever.py`는 실제 KURE-v1 대신 고정된 3차원 벡터를 반환하는 테스트용 `Embeddings`를 사용한다. 또한 Windows에서 임시 디스크 Chroma 파일 삭제 시 `WinError 32`가 발생하여, 테스트에서는 `chromadb.EphemeralClient()`를 사용하는 **메모리 기반 ChromaDB**로 변경했다. 실제 `data/chroma_samsung/`은 수정하지 않는다.

검증 항목:

- 최초 색인 및 문서 수 확인
- 재실행 시 기존 컬렉션 재사용
- 관련 청크 검색과 출처·거리 반환
- Top-K 결과 수
- 빈 질문 및 잘못된 `k` 처리
- 미색인 저장소 검색 방지
- 중복 `chunk_id` 차단
- 기존 DB와 입력 청크 ID 불일치 차단

Chroma 단위 테스트:

```powershell
python -m unittest discover -s tests -p "test_chroma_retriever.py" -v
```

```text
Ran 9 tests in 0.197s

OK
```

전체 회귀 테스트:

```powershell
python -m unittest discover -s tests -v
```

```text
Ran 36 tests in 3.076s

OK
```

9개 단위 테스트와 전체 36개 회귀 테스트가 통과했다. 이 테스트는 색인·검색 기능을 검증하지만 KURE-v1의 실제 의미 검색 정확도를 증명하지는 않는다. 로컬 디스크 영구 저장과 재실행 재사용은 앞의 수동 실행으로 확인했다.

### 4-6. 확인된 제약 및 후속 작업

- 현재는 기존 컬렉션의 **청크 수와 ID만 비교**한다. 같은 ID로 청크 본문·메타데이터가 변경된 경우 자동으로 감지하거나 재색인하지 않는다.
- 입력 청크 수 또는 ID가 달라지면 안전을 위해 오류를 발생시킨다. 재색인 자동화는 추후 고려한다.
- 임베딩 모델을 변경하면 기존 벡터를 그대로 재사용하지 않도록 **모델별 컬렉션을 분리하고 새로 색인**해야 한다.
- `data/chroma_samsung/`은 로컬 생성 데이터이므로 Git 커밋 대상에서 제외한다. 팀원은 각자의 환경에서 색인을 실행해야 한다.
- 20개 검증 질문을 사용한 Hit@3·Hit@5 정량 평가는 10번에서 진행한다.

## 현재 Chroma 결과

- 임베딩 모델: `nlpai-lab/KURE-v1`, 실행 장치 자동 선택(CPU/CUDA)
- 입력 청크: 삼성전자 2025 연결재무제표·주석 294개
- 영구 저장: `data/chroma_samsung/`, 컬렉션 `samsung_2025_consolidated_kure_v1`
- 저장·재실행: 최초 294개 색인, 재실행 시 294개 재사용 확인
- 검색 방식: Chroma 유사도 검색, 기본 Top-5
- 매출액 질문: Gold page 85쪽을 1위로 검색
- Chroma 단위 테스트: 9개 통과
- 전체 회귀 테스트: 36개 통과
- 정량 검색 평가·자동 재색인: 미구현, 후속 단계에서 검토

## 5. MMR 검색기

MMR(Maximal Marginal Relevance)은 질문과의 관련성뿐 아니라 검색 결과 간 다양성을 고려해 청크를 선택한다. 8번에서 생성한 Chroma 컬렉션을 재사용하므로 새로운 색인이나 문서 임베딩은 필요하지 않다.

### 5-1. 구현 방식과 설정

`chroma_retriever.py`의 `search_mmr()`에서 Chroma의 `as_retriever(search_type="mmr")`를 호출한다. 수업 `2_Retriever.ipynb`의 MMR 검색 예제를 기반으로 구현했다.

| 설정 | 기본값 | 의미 |
| --- | ---: | --- |
| `k` | 5 | 최종 반환 청크 개수 |
| `fetch_k` | 20 | 다양성을 고려하기 전에 수집하는 후보 청크 개수 |
| `lambda_mult` | 0.5 | 관련성과 다양성의 균형(0~1) |

질문 공백, `k <= 0`, `fetch_k < k`, `lambda_mult` 범위 이탈 또는 미색인 Chroma 컬렉션은 오류로 처리한다. 검색 결과는 기존 검색기와 같이 `rank`, `chunk_id`, `text`, `source_file`, `pdf_page`, `metadata`를 반환한다.

### 5-2. 실행 명령 및 결과

```powershell
python scripts/search_mmr.py --question "2025년 연결 기준 매출액은 얼마인가?" --top-k 5 --fetch-k 20 --lambda-mult 0.5
```

실제 실행 결과의 상위 5개 PDF 물리 페이지는 다음과 같다.

| 순위 | PDF 물리 페이지 | 검색 내용 |
| ---: | ---: | --- |
| 1 | 85 | 연결 손익계산서·매출액 |
| 2 | 146 | 인수·약정 관련 설명 |
| 3 | 195 | 보고기간 후 사건 |
| 4 | 193 | 사업결합 관련 주석 |
| 5 | 151 | 주식기준보상 관련 주석 |

매출액 질문의 Gold page **85쪽을 1위로 검색**하여 이 질문에서는 Hit@3·Hit@5에 모두 성공했다. 다만 나머지 결과에는 질문과 직접 관련성이 낮은 주석도 포함됐다. 다양성 설정의 장단점은 10번 전체 평가 후 11번에서 검토한다.

### 5-3. 단위 테스트

`tests/test_mmr_retriever.py`에서는 `MagicMock`으로 실제 Chroma와 임베딩 모델 로드를 대체한다. MMR 옵션 전달, 반환 필드, 빈 질문·잘못된 `k`·`fetch_k`·`lambda_mult`, 미색인 컬렉션 처리를 확인한다.

```powershell
python -m unittest discover -s tests -p "test_mmr_retriever.py" -v
```

```text
Ran 7 tests in 0.005s

OK
```

이 테스트는 MMR 검색 호출과 반환 형식의 검증이며, MMR 알고리즘의 의미 검색 성능을 증명하지는 않는다.

## 6. Hybrid-RRF 검색기

Hybrid-RRF는 BM25 키워드 검색과 Chroma 벡터 유사도 검색을 각각 실행한 다음, 두 결과의 **순위**를 결합한다. 검색 점수의 척도가 서로 달라도 병합할 수 있도록 Reciprocal Rank Fusion(RRF)을 사용한다.

### 6-1. 구현 방식과 설정

`hybrid_retriever.py`는 `search_hybrid()`와 `rrf_merge()`로 구성한다.

1. `*_chunks.json`에서 BM25 검색기를 생성한다.
2. BM25와 기존 Chroma 컬렉션에서 각각 후보 청크를 검색한다.
3. 동일한 `chunk_id`를 하나의 청크로 합치고 검색 순위별 RRF 점수를 더한다.
4. RRF 점수 내림차순, 동점이면 `chunk_id` 오름차순으로 정렬해 Top-K를 반환한다.

RRF 점수는 `score(d) = Σ[1 / (rank_constant + rank_i(d))]`로 계산한다. 검색기 내부에 같은 청크 ID가 중복 등장하면 해당 검색기의 점수는 한 번만 반영한다.

| 설정 | 기본값 | 의미 |
| --- | ---: | --- |
| `k` | 5 | 최종 반환 청크 개수 |
| `candidate_k` | 10 | BM25와 Chroma 각각의 후보 검색 개수 |
| `rank_constant` | 60 | 순위별 가산 점수 계산 상수 |

RRF 결과에는 공통 출처 필드와 `rrf_score`를 반환한다. 점수는 검색 순위 융합을 위한 상대값이며 정답 확률이 아니다.

### 6-2. 실행 명령 및 결과

```powershell
python scripts/search_hybrid.py --question "2025년 연결 기준 매출액은 얼마인가?" --top-k 5 --candidate-k 10
```

실제 실행 결과는 다음과 같다.

| 순위 | PDF 물리 페이지 | RRF 점수 | 검색 내용 |
| ---: | ---: | ---: | --- |
| 1 | 85 | 0.016393 | 연결 손익계산서·매출액 |
| 2 | 125 | 0.016393 | 관계기업 투자 현황 |
| 3 | 86 | 0.016129 | 연결 포괄손익계산서 |
| 4 | 129 | 0.016129 | 유형자산 관련 소송 |
| 5 | 89 | 0.015873 | 연결 현금흐름표 |

매출액 질문의 Gold page **85쪽을 1위로 검색**했다. 85쪽과 125쪽의 점수가 같은 이유는 각 청크가 서로 다른 검색기에서 1위로 검색됐기 때문이다. 이때 `chunk_id` 기준의 동점 정렬 때문에 85쪽이 먼저 나타난 것으로, RRF가 본문을 읽어 85쪽의 관련성이 더 높다고 판단한 것은 아니다.

현재 결과에는 BM25가 찾은 직접 관련성이 낮은 문서도 포함돼 있다. Hybrid가 다른 전략보다 우수한지는 20문항 평가가 필요하다.

### 6-3. 단위 테스트

`tests/test_hybrid_retriever.py`에서는 RRF 계산 함수를 직접 검증하고, BM25·Chroma 호출은 Mock 결과로 대체한다.

- 중복 청크의 점수 합산과 중복 제거
- 순위별 RRF 점수 및 정렬
- 점수 동점 시 `chunk_id` 정렬
- Top-K 적용 및 잘못된 매개변수 처리
- BM25·Chroma 검색기 호출과 결과 병합

```powershell
python -m unittest discover -s tests -p "test_hybrid_retriever.py" -v
```

```text
Ran 9 tests in 0.002s

OK
```

## 7. 네 가지 검색 방식 비교 및 9번 최종 검증

동일 질문 `2025년 연결 기준 매출액은 얼마인가?`를 대상으로 수동 검색 결과를 비교했다.

| 순위 | BM25 | Chroma Similarity | MMR | Hybrid-RRF |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 125 | **85** | **85** | **85** |
| 2 | 129 | 86 | 146 | 125 |
| 3 | 149 | 89 | 195 | 86 |
| 4 | 129 | 146 | 193 | 129 |
| 5 | 149 | 87 | 151 | 89 |

Gold page는 **PDF 물리 페이지 85쪽**이다. 이 질문에서는 BM25가 Hit@3·Hit@5에 실패했고, Chroma Similarity·MMR·Hybrid-RRF는 모두 성공했다. **한 질문의 관찰 결과로 전체 검색기 우열을 단정하지 않는다.**

MMR·Hybrid 테스트 추가 후 전체 회귀 테스트를 실행했다.

```powershell
python -m unittest discover -s tests -v
```

```text
Ran 52 tests in 2.515s

OK
```

- MMR 단위 테스트: 7개 통과
- Hybrid-RRF 단위 테스트: 9개 통과
- 전체 회귀 테스트: 52개 통과
- 전체 평가 질문 20개에 대한 정량 평가는 10번에서 별도로 진행

## 8. Hit@3·Hit@5 검색 성능 평가

`evaluation/dataset/eval_retrieval.json`의 검증 완료 질문 20개를 사용해 BM25, Chroma Similarity, MMR, Hybrid-RRF를 동일한 청크 데이터에서 평가했다. 이는 **정답 근거 페이지를 검색하는 성능**을 측정하는 단계이며, 생성된 답변이나 금액의 정확도를 측정하는 단계는 아니다.

### 8-1. 평가 데이터와 판정 기준

- 검색 데이터: 삼성전자 2025 사업보고서의 연결재무제표·연결 주석 294개 청크
- 평가 질문: 20개, 모두 `annotation_status: verified`
- 질문 구성: 수치형 8개, 비교형 4개, 주석 세부 4개, 서술형 4개
- 근거 판정: 검색 결과의 `source_file + pdf_page`와 `gold_sources` 비교
- 평가 지표: Hit@3, Hit@5

질문별 Gold source 중 하나가 상위 K개 **청크 검색 결과**에 포함되면 해당 질문을 Hit로 판정한다. Gold page가 여러 개인 경우에도 일치하는 페이지가 하나 이상 있으면 성공으로 처리한다. `pdf_page`는 PDF 물리 페이지 번호다.

`Hit@K = 정답 근거를 Top-K에서 찾은 질문 수 / 전체 평가 질문 수`

예를 들어 S04(2025년 연결 매출액)의 Gold source는 `20260310002820.pdf`의 85쪽이다. 검색 결과 상위 3개 청크 중 해당 출처가 있으면 Hit@3 성공이다.

### 8-2. 평가 지표 구현과 테스트

`evaluation/retrieval_metrics.py`에 다음 함수를 구현했다.

| 함수 | 역할 |
| --- | --- |
| `load_eval_dataset()` | JSON 평가셋 로드 및 `verified` 질문 검증 |
| `is_hit()` | 결과 상위 K개의 `source_file + pdf_page`가 Gold source와 일치하는지 판정 |
| `calculate_hit_rate()` | 질문별 Hit 결과를 집계하여 전체 성공률 계산 |

평가 지표 단위 테스트:

```powershell
python -m unittest discover -s tests -p "test_retrieval_metrics.py" -v
```

로컬 검증 결과 **7개 통과**. 평가셋 원본에서도 `verified` 질문 **20개를 정상 로드**했다. 해당 단계까지 전체 회귀 테스트는 **59개 통과**했다.

### 8-3. 네 가지 검색 방식 자동 평가

`scripts/evaluate_retrieval.py`는 평가셋의 질문을 네 가지 검색기에 순서대로 입력한다. BM25 검색기와 KURE-v1 임베딩 모델·Chroma 저장소는 평가 시작 시 각각 한 번 생성하거나 로드하고, 질문마다 재사용한다.

| 검색 방식 | 평가 설정 |
| --- | --- |
| BM25 | Top-5 (Hybrid용 후보 Top-10 별도 확보) |
| Chroma Similarity | Top-5 (Hybrid용 후보 Top-10 별도 확보) |
| MMR | `k=5`, `fetch_k=20`, `lambda_mult=0.5` |
| Hybrid-RRF | `k=5`, `candidate_k=10`, `rank_constant=60` |

각 검색기는 기존의 동일한 294개 청크를 사용한다. 평가 전에 Chroma 컬렉션의 문서 수와 `chunk_id` 집합이 입력 데이터와 일치하는지 확인한다. 다만 같은 ID로 본문이 수정된 경우까지 감지하는 기능은 아직 없다.

실행 명령:

```powershell
python scripts/evaluate_retrieval.py
```

결과는 `evaluation/results/retrieval_baseline.json`에 기록한다.

- `config`: 임베딩 모델과 검색 매개변수
- `summary`: 검색 방식별 Hit@3·Hit@5, 성공 질문 수, 전체 질문 수
- `details`: 질문별 Hit 여부 및 상위 청크의 ID·출처 파일·PDF 페이지

### 8-4. 삼성전자 2025 베이스라인 평가 결과

로컬에서 20개 질문을 평가한 실제 결과는 다음과 같다.

| 검색 방식 | Hit@3 | Hit@5 | Hit@3 성공 | Hit@5 성공 |
| --- | ---: | ---: | ---: | ---: |
| BM25 | 30.0% | 35.0% | 6/20 | 7/20 |
| Chroma Similarity | **90.0%** | **90.0%** | **18/20** | **18/20** |
| MMR | 75.0% | 75.0% | 15/20 | 15/20 |
| Hybrid-RRF | 85.0% | **90.0%** | 17/20 | **18/20** |

현재 평가셋에서는 Chroma Similarity가 Hit@3·Hit@5에서 가장 높은 수치를 보였다. Hybrid-RRF는 Hit@5에서 Chroma와 같았지만 Hit@3에서 5%p 낮았고, BM25의 성능은 상대적으로 낮았다.

이 수치는 **삼성전자 2025년 자료·검증된 20문항·현재 검색 설정**에 한정된다. 다른 기업이나 문서에서도 같은 결과가 나온다고 일반화하지 않는다.

### 8-5. 평가 스크립트 테스트 및 최종 회귀 테스트

`tests/test_evaluate_retrieval.py`는 실제 KURE-v1을 로드하지 않고 Mock 검색 결과로 평가 실행 흐름을 검증한다.

- 네 검색 방식이 모두 평가되고 문항 수가 집계되는지
- Hit@3·Hit@5 비율 및 성공 개수가 정확한지
- Chroma 청크 수·ID 불일치 시 오류가 발생하는지
- 결과 JSON이 지정 경로에 저장되고 내용이 유지되는지

```powershell
python -m unittest discover -s tests -p "test_evaluate_retrieval.py" -v
python -m unittest discover -s tests -v
```

로컬 실행 결과:

```text
평가 실행 스크립트 단위 테스트: 5개 통과
전체 회귀 테스트: 64개 통과
```

단위 테스트는 평가 코드의 동작을 검증한다. 실제 검색 성능은 앞의 20문항 평가 결과로 별도 확인했다.

### 8-6. 기준선 보존 및 남은 한계

- `evaluation/results/retrieval_baseline.json`은 11번 개선 실험 이전 결과로 Git에 보존한다.
- 개선 후 평가는 별도 파일(예: `retrieval_improved.json`)로 저장하고, 기준선 파일을 덮어쓰지 않는다.
- 현재 Hit@K는 **청크 순위 기준**이다. 동일 페이지에서 나온 복수 청크를 하나의 페이지로 합쳐 평가하지 않는다.
- 정답 페이지 검색 여부와 실제 답변의 수치·기간·연결 기준 정확성은 구분해서 평가해야 한다.
- 검색 실패의 구체적인 원인은 아직 확정하지 않았다. 11번에서 문항별 결과와 원문을 대조해 분석한다.

## 다음 작업

1. **11번 검색 실패 분석 및 개선**: `retrieval_baseline.json`에서 실패 문항을 추출하고, 토큰화·청킹·검색 전략·출처 기준 문제를 구분해 개선 실험을 수행한다.
2. **12번 최종 정리**: 개선 전후 지표, 실패 사례, 실행 명령, 모델·검색 설정, 최종 회귀 테스트를 문서와 README에 정리한다.
