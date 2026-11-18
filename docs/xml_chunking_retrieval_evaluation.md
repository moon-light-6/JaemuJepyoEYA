# DART XML 청킹과 검색 평가

이 문서는 PDF 기반 검색과 별개로, DART 원본 ZIP 안의 감사보고서 XML을 문서화하고 청킹·검색 평가하는 흐름을 설명한다. XML은 PDF 쪽번호 대신 원본 XML의 파일명과 XPath를 근거 위치로 사용한다.

## 1. 처리 흐름

```text
원본 DART ZIP
  → 감사보고서/연결감사보고서 선택
  → 재무제표 표 + 번호별 주석 문서화
  → XML 청크 생성
  → draft 평가셋 생성 및 사람 검증
  → BM25 / Chroma / MMR / Hybrid 평가
```

연결은 `consolidated`, 별도는 `separate`를 사용한다. 두 기준은 서로 다른 XML 문서·청크·평가셋·Chroma 컬렉션을 가진다.

## 2. 산출물 경로

XML 산출물은 PDF 산출물과 섞이지 않도록 아래처럼 저장한다.

```text
data/processed/xml/dart/{corp_code}/{fiscal_year}/{scope}/
  {rcept_no}_xml_documents.json
  {rcept_no}_xml_chunks.json
  {rcept_no}_xml_pipeline.json

evaluation/dataset/xml/{corp_code}/{fiscal_year}/{scope}/
  {rcept_no}_retrieval.json

evaluation/results/xml/{corp_code}/{fiscal_year}/{scope}/
  {rcept_no}_retrieval.json
```

`*_xml_documents.json`의 각 항목에는 `source_file`, `source_xpath`, `source_element_index`가 있다. 이 값이 사람 검증과 XML 검색 평가의 위치 정답이다.

## 3. 삼성전자 연결 실행 예시

원본 ZIP이 `data/raw/dart/00126380/2025/20260310002820.zip`에 있어야 한다.

```zsh
python scripts/run_pipeline_xml.py \
  --company "삼성전자" \
  --corp-code 00126380 \
  --fiscal-year 2025 \
  --rcept-no 20260310002820 \
  --statement-scope consolidated \
  --make-draft-dataset
```

`--make-draft-dataset`은 문서화·청킹까지 포함해 실행하고, OpenDART 재무제표 계정과 XML 후보 위치를 연결한 최대 20개의 `draft` 문항을 만든다. 기업별 XML에서 후보를 못 찾으면 20개보다 적을 수 있다.

문서와 청크만 만들고 싶을 때는 다음처럼 실행한다. `--prepare-only`와 `--make-draft-dataset`은 함께 쓸 수 없다.

```zsh
python scripts/run_pipeline_xml.py \
  --company "삼성전자" \
  --corp-code 00126380 \
  --fiscal-year 2025 \
  --rcept-no 20260310002820 \
  --statement-scope consolidated \
  --prepare-only
```

## 4. draft 검증

자동 생성 문항은 정답이 확정된 것이 아니다. 각 문항의 `gold_sources.source_xpath`와 `candidate_sources`를 XML 문서 원문에서 대조한다.

1. `evaluation/dataset/xml/.../{rcept_no}_retrieval.json`을 연다.
2. `source_file`, `source_xpath`, 금액·단위·연결/별도 기준을 확인한다.
3. 맞으면 `annotation_status`를 `draft`에서 `verified`로 바꾼다.

브라우저에서 더 읽기 좋게 검증하려면 [xml_document_viewer.html](./xml_document_viewer.html)을 열고, 같은 scope의 `*_xml_documents.json`을 선택한다. 검색창은 `자산총계`처럼 공백 없이 입력해도 `자 산 총 계` 표기를 찾고, 목록에 XML 요소 인덱스와 일치 횟수를 보여 준다.

## 5. 검색 평가

하나 이상의 문항을 `verified`로 바꾼 뒤, 같은 명령에서 `--make-draft-dataset`만 뺀 채 실행한다.

```zsh
python scripts/run_pipeline_xml.py \
  --company "삼성전자" \
  --corp-code 00126380 \
  --fiscal-year 2025 \
  --rcept-no 20260310002820 \
  --statement-scope consolidated
```

청킹 규칙을 바꾼 뒤에는 기존 XML Chroma 컬렉션만 다시 만들도록 `--rebuild-index`를 추가한다. 원본 ZIP과 평가셋은 삭제하지 않는다.

```zsh
python scripts/run_pipeline_xml.py \
  --company "삼성전자" \
  --corp-code 00126380 \
  --fiscal-year 2025 \
  --rcept-no 20260310002820 \
  --statement-scope consolidated \
  --rebuild-index
```

## 6. 지표

| 지표 | 성공 조건 |
| --- | --- |
| 위치@3/@5 | 상위 K개 중 `source_file`과 `source_xpath`가 Gold source와 일치 |
| 값@3/@5 | 상위 K개 청크 본문 안에 기대값이 있음 |
| 위치+값@3/@5 | 같은 청크가 Gold XML 위치이면서 기대값도 포함 |

점수가 낮으면 먼저 XML 문서 단위가 너무 큰지, 표가 답 행 기준으로 잘렸는지, 질문의 계정명과 원문 표기가 다른지 확인한다. 현재 기본 청킹은 문서당 600자, 중복 100자이며, 표의 행 구조를 더 엄밀히 보존하는 청킹은 후속 개선 과제다.
