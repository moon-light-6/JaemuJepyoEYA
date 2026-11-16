# 검색 데이터 준비와 평가셋

> 공식 PDF 수집 이후의 페이지 표준화·청킹·검증·평가셋 준비 과정을 다룹니다.

이 문서는 공식 PDF 수집이 끝난 뒤, 검색에 쓸 데이터를 만들고 검색 성능을 평가하기까지의 흐름을 정리한 문서다.

현재 MVP는 삼성전자 2025 사업보고서 중 연결재무제표와 연결 주석이 있는 PDF 물리 페이지 83~195만 검색 대상으로 사용한다.

## 만들어지는 파일

| 파일 | 역할 |
| --- | --- |
| `data/processed/dart/{corp_code}/{fiscal_year}/{rcept_no}_pages.json` | 검색 대상 페이지의 원문과 출처 메타데이터 |
| `data/processed/dart/{corp_code}/{fiscal_year}/{rcept_no}_chunks.json` | 페이지를 검색용 청크로 나눈 결과 |
| `data/processed/dart/{corp_code}/{fiscal_year}/{rcept_no}_chunks_validation.json` | 청크 품질 점검 결과 |
| `evaluation/dataset/eval_retrieval.json` | 검색기를 채점할 질문·정답·Gold page 20개 |

`pdf_page`는 문서 하단의 인쇄 페이지가 아니라 PDF 파일 기준의 물리 페이지 번호다.

## 1. 검색 대상 페이지 표준화

`scripts/prepare_pages.py`는 `*_pdf_pages.json`에서 필요한 페이지 범위만 골라 검색용 페이지 JSON을 만든다. 회사명·연도·접수번호·페이지 범위는 실행 인자로 받으므로 다른 기업에도 그대로 쓸 수 있다.

페이지 하나에는 아래 정보가 들어간다.

```text
page_id, text, company, fiscal_year, statement_scope,
document_type, source_file, pdf_page, section
```

### 연결 구간 경계 쪽 자동 처리

연결 구간의 첫 쪽과 끝 쪽에는 연결이 아닌 내용이 같이 있는 경우가 있다(예: 시작 쪽 위쪽의 별도 요약재무정보, 끝 쪽 아래쪽의 별도재무제표 시작). `--statement-scope consolidated`이면 `prepare_pages.py`가 원본 PDF(`data/raw/dart/{corp_code}/{fiscal_year}/{rcept_no}.pdf`)에서 아래 제목이 **단독 한 줄로 정확히 한 번** 나오는 위치를 찾아 연결 부분만 남긴다. 따라서 평소 실행 명령은 그대로 쓰면 된다.

| 경계 | 제목 | 처리 |
| --- | --- | --- |
| 시작 쪽 | `2. 연결재무제표` | 제목 위쪽에 본문이 있으면 제목부터 아래쪽만 사용 |
| 끝 쪽 또는 바로 다음 쪽 | `4. 재무제표` | 제목 위쪽에 본문(연결 주석의 끝부분)이 있으면 위쪽만 사용. 바로 다음 쪽이면 범위에 자동 추가 |

- 경계 쪽에 연결이 아닌 내용이 없으면 아무것도 바꾸지 않는다(에코프로가 해당).
- 자동 처리 결과는 `[자동 경계 처리] ...` 로 출력되므로 실행 후 확인한다.
- 직접 지정하려면 `--cut-above "141:4. 재무제표"`, `--cut-below "54:2. 연결재무제표"`를 쓴다. 같은 쪽은 직접 지정이 우선한다.
- 예전처럼 쪽 전체를 쓰려면 `--no-auto-cut`을 붙인다.
- 제목이 위 형식과 다른 PDF에서는 자동으로 찾지 못하고 쪽 전체를 쓴다. 이 경우 직접 지정한다. (삼성전자 PDF는 이 컴퓨터에 없어 확인하지 못했다.)

프로젝트 루트에서 실행한다. 아래 명령은 macOS/zsh 기준이다.

```bash
source venv/bin/activate

python scripts/prepare_pages.py \
  --company "삼성전자" \
  --corp-code 00126380 \
  --fiscal-year 2025 \
  --rcept-no 20260310002820 \
  --statement-scope consolidated \
  --section-range "83-90:연결재무제표" \
  --section-range "91-195:연결재무제표 주석"
```

정상 실행 결과:

```text
입력 PDF 페이지 수: 573
선택된 페이지 수: 113
출력 파일: .../20260310002820_pages.json
```

### Windows 명령 프롬프트(CMD)

Windows의 **명령 프롬프트(CMD)** 에서는 가상환경을 아래처럼 활성화한다. CMD의 줄바꿈 문자는 `^`다. PowerShell의 백틱(`)과 섞어 쓰지 않는다.

```cmd
venv\Scripts\activate.bat

python scripts\prepare_pages.py ^
  --company "삼성전자" ^
  --corp-code 00126380 ^
  --fiscal-year 2025 ^
  --rcept-no 20260310002820 ^
  --statement-scope consolidated ^
  --section-range "83-90:연결재무제표" ^
  --section-range "91-195:연결재무제표 주석"
```

## 2. 청킹

`scripts/build_chunks.py`는 페이지 JSON을 읽어 긴 본문을 검색용 청크로 나눈다. 수업 코드와 같은 `RecursiveCharacterTextSplitter`를 사용한다.

- 기본 청크 크기: 600자
- 기본 청크 중복: 100자
- 페이지 경계를 넘어 청크를 합치지 않음
- 청크는 원래 페이지의 회사·연도·연결 기준·PDF 페이지·섹션 메타데이터를 유지함

```bash
python scripts/build_chunks.py \
  --input-path data/processed/dart/00126380/2025/20260310002820_pages.json \
  --output-path data/processed/dart/00126380/2025/20260310002820_chunks.json
```

`--chunk-size`, `--chunk-overlap`은 생략하면 600, 100을 사용한다. 청킹 방식을 비교할 때만 직접 값을 넣는다.

정상 실행 결과:

```text
원본 페이지 수: 113
만들어진 청크 수: 294
청크 크기 / 중복: 600 / 100
```

Windows CMD에서는 아래 명령을 사용한다.

```cmd
python scripts\build_chunks.py ^
  --input-path data\processed\dart\00126380\2025\20260310002820_pages.json ^
  --output-path data\processed\dart\00126380\2025\20260310002820_chunks.json
```

청크 하나에는 `chunk_id`가 추가된다.

```json
{
  "chunk_id": "00126380_2025_consolidated_0083_01",
  "text": "검색할 본문 일부",
  "company": "삼성전자",
  "fiscal_year": 2025,
  "statement_scope": "consolidated",
  "source_file": "20260310002820.pdf",
  "pdf_page": 83,
  "section": "연결재무제표"
}
```

## 3. 청크 품질 점검

`scripts/check_chunks.py`는 원래 페이지 JSON과 청크 JSON을 비교한다. 데이터를 수정하지 않고 검증 결과만 저장한다.

```bash
python scripts/check_chunks.py \
  --pages-path data/processed/dart/00126380/2025/20260310002820_pages.json \
  --chunks-path data/processed/dart/00126380/2025/20260310002820_chunks.json \
  --output-path data/processed/dart/00126380/2025/20260310002820_chunks_validation.json
```

점검 항목:

- 빈 청크, 중복 `chunk_id`, 메타데이터 누락
- 청크 길이가 설정한 최대 크기를 넘는지
- 청크의 `page_id`와 메타데이터가 원래 페이지와 일치하는지
- 청크 텍스트가 원래 해당 페이지에서 왔는지
- 선택한 모든 페이지에 청크가 하나 이상 있는지

정상 실행 결과:

```text
페이지 수: 113
청크 수: 294
청크가 포함된 페이지 수: 113
검증 상태: passed
```

Windows CMD에서는 아래 명령을 사용한다.

```cmd
python scripts\check_chunks.py ^
  --pages-path data\processed\dart\00126380\2025\20260310002820_pages.json ^
  --chunks-path data\processed\dart\00126380\2025\20260310002820_chunks.json ^
  --output-path data\processed\dart\00126380\2025\20260310002820_chunks_validation.json
```

## 4. 평가 질문과 Gold page

`evaluation/dataset/eval_retrieval.json`은 검색기를 채점하는 문제지다. 질문마다 정답 값과 사람이 PDF 원문으로 확인한 Gold page를 기록한다.

현재 평가셋은 정확한 수치 8개, 기간 비교 4개, 주석 세부 4개, 서술형 근거 4개로 총 20문제다. 금액 질문에는 `unit: "백만원"`을 넣는다. 같은 답이 여러 페이지에 직접 보이면 `gold_sources`에 모두 기록할 수 있고, 검색 결과가 그중 하나를 찾으면 정답으로 처리한다.

평가셋을 처음 보는 사람을 위한 자세한 설명은 [`evaluation/dataset/annotation_guide.md`](../evaluation/dataset/annotation_guide.md)에 있다.

## 현재 삼성전자 2025 결과

- 검색 대상: PDF 물리 페이지 83~195, 총 113페이지
- 생성된 청크: 294개
- 청크 검증: `passed`
- 평가 질문: 20개, 모두 `verified`

## 다음 작업: 검색기와 성능 평가

1. **BM25 검색기**: `*_chunks.json`을 읽어 질문과 관련된 청크, 본문 일부, `pdf_page`를 반환한다.
2. **Chroma 벡터 검색기**: 같은 청크를 임베딩해 자연어 의미가 비슷한 내용을 찾고, `data/chroma_samsung/`에 저장한다.
3. **MMR·Hybrid-RRF 비교**: BM25, Vector Similarity, MMR, BM25+Vector Hybrid-RRF를 같은 질문에 실행한다.
4. **Hit@3·Hit@5 평가**: 검색 결과의 `source_file + pdf_page`가 `eval_retrieval.json`의 `gold_sources` 중 하나와 일치하는지 비교한다.
5. **실패 사례 보정**: 연도·연결/별도 기준 혼동, 표 텍스트 문제, 페이지 중복, 근거가 없는 질문을 분류하고 청크 크기·필터·검색 방식을 조정한다.
6. **결과 정리**: 방식별 Hit@3·Hit@5와 실패 사례를 README 또는 노트북에 남기고, 실제 점수가 가장 높은 방식을 기본 검색기로 정한다.

전체 회귀 테스트:

```bash
python -m unittest discover -s tests -v
```

Windows CMD에서는 같은 명령을 그대로 실행하면 된다.

```cmd
python -m unittest discover -s tests -v
```

현재는 청크 검증 테스트까지 포함해 18개 테스트가 통과해야 한다.
