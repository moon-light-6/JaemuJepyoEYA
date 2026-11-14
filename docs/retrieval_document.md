# 검색기 구현 및 성능 평가

> 공식 PDF 청킹 이후 BM25·Chroma·MMR·Hybrid-RRF 검색기를 만들고, 검색 성능을 검증하는 과정을 다룬다.

이 문서는 `docs/chunking_document.md`에서 준비한 검색용 청크와 평가셋을 입력으로 사용한다. 현재는 **7번 BM25 키워드 검색기의 기본 구현과 테스트** 까지 기록했으며, Chroma와 하이브리드 검색 및 20문항 전체 평가는 후속 단계에서 추가한다.

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

## 다음 작업

1. **8번 Chroma 벡터 검색기**: 동일한 청크를 임베딩해 Chroma에 적재하고 의미 기반 검색을 구현한다.
2. **9번 MMR·Hybrid-RRF**: BM25·벡터 유사도·MMR·Hybrid-RRF 검색 결과를 비교한다.
3. **10번 Hit@3·Hit@5 평가**: `eval_retrieval.json`의 20개 질문을 동일 조건에서 실행해 Gold page 검색 성공률을 계산한다.
4. **11번 검색 실패 개선**: 토큰화·청킹·검색 전략을 수정하고 개선 전후 결과를 비교한다.
5. **12번 문서화**: 모델·검색 설정, 실행 순서, 검색 품질 비교표, 실패 사례, 테스트 결과를 최종 정리한다.

검색 성능을 비교할 때는 `evaluation/dataset/annotation_guide.md`의 기준에 따라 검색 결과의 `source_file + pdf_page`가 `gold_sources` 중 하나와 일치하는지 확인한다.
