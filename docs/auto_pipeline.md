# 여러 기업 자동 처리 (`scripts/run_pipeline.py`)

기업명이나 종목코드만 넣으면 공식 PDF 수집부터 Hit@3·Hit@5 평가까지 한 번에 실행한다.
기존 스크립트(`prepare_pages.py` → `build_chunks.py` → `check_chunks.py` → `build_chroma.py`
→ `evaluate_retrieval.py`)를 같은 순서로 호출하고, 사람이 손으로 넣던 값만 자동으로 채운다.

| 값 | 자동으로 얻는 방법 |
| --- | --- |
| `corp_code` | OpenDART 고유번호 파일(`corpCode.xml`)을 `data/raw/dart/CORPCODE.xml`에 30일간 캐시해 기업명·종목코드로 조회 |
| `rcept_no` | OpenDART 공시목록. 정정공시로 후보가 여러 건이면 최신 접수본 (`--report-choice original`로 최초본) |
| `dcm_no` | DART 공시 뷰어 `dsaf001/main.do?rcpNo=` 페이지에서 추출 |
| 페이지 범위 | PDF 텍스트에서 `2. 연결재무제표` / `3. 연결재무제표 주석` / `4. 재무제표` 제목이 단독 줄로 있는 쪽을 경계로 판정. 연결재무제표가 없으면 `4. 재무제표` / `5. 재무제표 주석` / `6. 배당에 관한 사항`으로 별도 기준 사용 |
| 평가셋 | OpenDART 재무제표 API(`fnlttSinglAcntAll`) 금액을 PDF 단위로 바꿔, 같은 행 계정명과 함께 적힌 쪽을 Gold page로 기록 |

`configs/companies.yaml`에 `rcept_no`, `official_pdf.dcm_no`가 있으면 그 값을 우선한다.

## 실행

```powershell
# 1) 자동 조회값이 기존에 손으로 확인한 값과 같은지 먼저 확인 (수집·색인 없음)
python scripts/run_pipeline.py --companies 삼성전자 HLB 에코프로 --resolve-only

# 2) 기업 하나 / 여러 개 / companies.yaml 전체
python scripts/run_pipeline.py --companies 파마리서치
python scripts/run_pipeline.py --companies 파마리서치 클래시스 214150
python scripts/run_pipeline.py --from-config
```

주요 옵션

| 옵션 | 의미 |
| --- | --- |
| `--fiscal-year 2025` | 사업연도 (기본 2025) |
| `--scope auto\|consolidated\|separate` | 기본 auto: 연결이 있으면 연결 |
| `--rebuild` | PDF 텍스트 추출과 해당 기업 Chroma 컬렉션을 다시 만든다 (청크 범위·크기를 바꿨을 때) |
| `--regenerate-eval` | 자동 생성 평가셋만 다시 만든다. 사람이 만든 문항이 섞인 평가셋은 건드리지 않는다 |
| `--with-xml` | XML 원본 ZIP 수집과 `report_parser`·`table_parser`·`notes_parser`·`check_documents`도 실행 (실패해도 평가는 계속) |
| `--skip-eval` | Chroma 색인까지만 |
| `--chunk-size`, `--chunk-overlap` | 기본 600 / 100 |

## 결과 파일

| 파일 | 내용 |
| --- | --- |
| `evaluation/dataset/eval_{corp}_{year}_{scope}_{rcept}.json` | 자동 생성 평가셋. 이미 있으면 새로 만들지 않고 그대로 사용 |
| `evaluation/results/retrieval_{corp}_{year}_{scope}_{rcept}.json` | 기업별 문항 단위 평가 결과 (`evaluate_retrieval.py` 기본 경로) |
| `evaluation/results/retrieval_summary.csv` | 전체 기업 요약 (엑셀에서 바로 열림). 같은 기업·연도를 다시 돌리면 해당 행만 갱신 |
| `data/processed/dart/{corp}/{year}/{rcept}_pipeline.json` | 자동으로 정한 값(dcm_no 출처, 페이지 범위, 단위, 재무제표 종류별 쪽)의 기록 |
| `evaluation/dataset/{corp}/{year}/{scope}/{rcept}.json` | 자동 생성 평가셋. 이미 있으면 새로 만들지 않고 그대로 사용 |
| `evaluation/results/{corp}/{year}/{scope}/{rcept}.json` | 기업별 문항 단위 평가 결과 (`evaluate_retrieval.py` 기본 경로) |
| `evaluation/results/retrieval_summary.csv` | 전체 기업 요약 (엑셀에서 바로 열림). 같은 기업·연도를 다시 돌리면 해당 행만 갱신 |
| `data/processed/dart/{corp}/{year}/{scope}/{rcept}.json` | 자동으로 정한 값(dcm_no 출처, 페이지 범위, 단위, 재무제표 종류별 쪽)의 기록 |

## 자동 평가셋의 범위와 한계

- 자동으로 만드는 문항은 수치형 최대 8개(자산총계, 현금및현금성자산, 재고자산, 매출액, 영업이익,
  당기순이익, 영업활동현금흐름, 배당금 지급, 예비로 부채총계·자본총계)와 비교형 최대 4개다.
  PDF에서 값이 확인되지 않은 계정은 빠지므로 기업마다 문항 수가 다를 수 있다.
- 주석 세부·서술형 문항은 만들지 않는다. 사람이 만든 삼성전자 20문항과 문항 구성이 달라서
  점수를 그대로 비교하면 안 된다. 주석 검색 성능까지 보려면 같은 파일에 사람이 문항을 추가한다
  (추가하면 그 파일은 `mixed`로 표시되고 `--regenerate-eval`로도 덮어쓰지 않는다).
- 자동 문항은 `annotation_status: verified`, `annotation_method: auto_opendart_pdf_match`로
  기록한다. "OpenDART 값이 그 쪽에 같은 계정명과 함께 적혀 있음"을 기계적으로 확인했다는 뜻이다.
- 삼성전자 수동 평가셋과 비교하면 같은 계정 12문항 중 11문항의 Gold page가 같았다. 당기순이익은
  손익계산서(85쪽)에 더해 같은 값이 적힌 포괄손익계산서(86쪽)도 Gold page로 잡는다.
- 페이지 범위 자동 판정은 삼성전자·HLB·에코프로의 수동 범위와 모두 일치했다
  (`tests/test_auto_pipeline.py`).

## 자동 판정이 실패할 때

- `dcm_no`를 못 찾으면: DART 뷰어의 다운로드 창 주소에서 `dcm_no`를 확인해 `companies.yaml`의
  `official_pdf.dcm_no`(그리고 `rcept_no`)에 넣고 다시 실행한다.
- 페이지 범위를 못 찾으면: 오류 메시지에 제목이 발견된 쪽 번호가 나온다. 이 기업만 기존 방식대로
  `prepare_pages.py --section-range`를 직접 실행한다.
- 한 기업이 실패해도 나머지 기업은 계속 진행하고, 실패 사유는 요약 CSV의 `message`에 남는다.
