# 자연어 요청 기반 전체 실행 통합 및 E2E 테스트

- 테스트 일자: 2026.11.25 ~ 11.26
- 작업 브랜치: `feat/api-serving`
- 범위: 기업 식별 → 적재/미적재 분기 → 데이터 준비 → 보고서 생성 → 검증 → 결과 반환 → 임시 리소스 정리
- 제외: 프론트엔드 UI, 조사·청킹·검색·주장 추출·평가의 내부 로직, YAML·프롬프트·모델 설정

## 1. 결과 요약

세 기업의 서비스 상태는 모두 `partial_success`다. **`success`를 받은 실행은 없다.**
이 테스트는 연결 여부를 확인한 것으로, 보고서 정확도와 검증 성능을 보장하지 않는다.

### 1-1. 전체 기능 연결

| 항목 | 판정 | 근거 |
|---|---|---|
| 요청 하나로 전체 실행 | **성공** | 미적재 1개, 적재 2개 기업에서 식별부터 정리까지 끝까지 실행됨 |
| 영구 적재 없음 | **성공** | Supabase·로컬 Chroma 변화 없음. 공용 Chroma는 비교 가능한 구간에서 변화 없음(6장) |
| 임시 리소스 정리 | **성공** | 정상 종료와 강제 예외 모두 삭제됨 |
| 주장별 판정 결과 반환 | **부분 성공** | CJ대한통운 16건, 리노공업 1건 반환. **서진시스템은 0건이라 실패** |

### 1-2. 보고서 생성 및 검증 품질

| 항목 | 판정 | 근거 |
|---|---|---|
| 보고서 생성 | **부분 성공** | 3개 모두 생성. 숫자는 조회됐으나 변동 원인·리스크 설명이 부족 (5-3) |
| 주장 추출 | **불안정** | 16건, 0건, 1건. 응답이 출력 한도에서 잘림 (5-4) |
| 주장 판정 | 추출된 주장에서 정상 동작 | 17건 중 일치 16, 검증 불가 1 |

### 1-3. 실제 테스트로 확인된 기능

실제 OpenDART, Supabase, Chroma, OpenAI를 호출해 확인했다.

- 미적재 기업 전체 실행 (API POST → GET, 테스트 클라이언트)
- 적재 기업 전체 실행 (연결 1개, 별도 1개, 서비스 함수)
- 기업명·연도·기준 누락, 없는 이름, 동명 기업 되묻기
- 유사 이름이 적재 기업으로 바뀌지 않음 (식별까지)
- 공시 없음, Supabase 연결 실패 시 중단
- 공용 Chroma 연결 실패 시 임시 색인 준비와 검색, 사유 기록
- 실행 중 예외 발생 시 임시 Chroma와 폴더 정리
- uvicorn 서버의 접수, 상태 조회, 되묻기 응답(`202`, `200`)과 오류 응답(`404`, `422`)

### 1-4. 아직 확인하지 못한 기능

- `success` 상태로 끝나는 실제 실행
- 주장 추출 기본 설정(Qwen)과 Qwen 연결 실패 시의 실제 동작
- uvicorn 서버에서의 전체 실행
- 미적재 기업의 별도 기준 전체 실행
- 한 자원만 적재된 기업(`mixed`)의 조사·검증. 공용 Chroma 실패도 준비와 검색까지만 확인
- 별도 기준 검증은 리노공업 주장 1건에서만 확인
- E2E 이후 추가한 `claims_incomplete`의 실제 실행 (단위 테스트와 저장된 결과로만 확인)
- 실제 LLM을 쓰는 동시 요청. 기업 정보 격리와 순차 실행은 단위 테스트로 확인
- `429` 응답의 자동 테스트. 코드를 직접 실행해 11번째 요청이 거부되는 것은 확인
- 정리 후 `on_demand.py`를 사용하는 기존 `test_on_demand_*.py` 2개의 재실행. 불러오기만 확인
- 보고서 문장과 원문의 일치 여부 (사람 검토)

## 2. 아키텍처

**작업 전**

```text
test_on_demand_research.py (patch) → JSON 파일 → test_on_demand_verify.py (patch)
기업·공시번호 하드코딩, 생성과 검증을 따로 실행, API 없음
```

**작업 후**

```text
POST /api/research
 → research_routes    작업 접수, 메모리 상태, 전용 이벤트 루프 1개
 → research_service   전체 실행, 상태 판정, 정리
     ① identify  company_resolver   조건 추출 → OpenDART 정확 일치 → 공시 확정
     ② prepare   data_source        적재 확인 → 기존 저장소 또는 임시 데이터
     ③ research  기존 Research 그래프 (수정 없음)
     ④ verify    기존 검증 그래프 1회 (수정 없음)
     ⑤ cleanup   임시 Chroma 종료, 폴더 삭제
 → GET /api/research/{id}
```

기업·연도·기준·공시번호는 ①에서 `ResolvedRequest` 하나로 고정한다. 두 그래프의 결과가 이 값과 다르면 `context_mismatch`로 처리한다. 검증 그래프는 기준을 반환하지 않으므로 기업·연도·공시번호만 비교하고, 기준은 `RunAccountStore`로 맞춘다.

### 적재·미적재 처리

| 자원 | 확인 결과 | 처리 |
|---|---|---|
| 정형데이터 | 같은 공시의 행이 Supabase에 있음 | Supabase 사용 |
| | 행이 없거나 다른 공시만 있음 | OpenDART에서 수집, 조사와 검증이 함께 사용 |
| | **연결 실패** | **중단** (`storage_unavailable`) |
| 검색 색인 | 로컬 또는 공용 Chroma에 청크가 있음 | 기존 색인 사용 (로컬 우선, 기존 순서) |
| | 청크 0개, 없음, 다른 공시 | 임시 색인, `fallback_reason: not_loaded` |
| | **공용 연결 실패** | 임시 색인, `fallback_reason: shared_connection_failed` |

- 컬렉션 존재가 아니라 **청크 수**와 모든 청크의 기업·연도·기준·공시번호로 판단한다.
- `mode`: `loaded`(둘 다 기존), `on_demand`(둘 다 임시), `mixed`(하나만 임시)

### 재사용한 기존 구현

| 단계 | 함수·모듈 |
|---|---|
| 조건 추출 | `ClarifyExtraction`, `load_prompt("clarify")`, `ainvoke_structured` |
| 기업·공시 | `load_corp_codes`, `normalize_corp_name`, `search_business_reports`, `choose_report`, `industry_division_name` |
| 수집·청킹·색인 | `download_original_zip`, `build_xml_documents`, `build_xml_chunks`(v2), `build_xml_chroma`, `close_chroma_client`, `fetch_financial_accounts`, `to_number` |
| 검색 | `SearchToolCache`, `ResearchSearchSession`, `XMLDocumentSearchTool`, `shared_chroma_client` |
| 조사 | Research `build_graph`·`run_config`, `make_loop_researcher`, `FinancialAccountTool`, `EvidenceRegistry`, `Budget` |
| 검증 | 검증 `build_graph`·`run_config`, `AccountStore`, `fetch_rows` |

## 3. 변경 파일

| 파일 | 구분 | 변경 이유 |
|---|---|---|
| `src/fin_research/companies.py` | 수정 (+25/-1) | `resolve_context`와 `claim_extract`가 `load_companies()`를 직접 호출해 주입할 곳이 없음. `ContextVar`로 해당 실행의 기업만 전달하고, 설정하지 않으면 기존처럼 YAML 사용 |
| `src/fin_research/api/server.py` | 수정 (+4) | 라우터 등록. 기존 엔드포인트 유지 |
| `src/fin_research/api/schemas.py` | 수정 (추가) | 조사 요청·응답 형식 |
| `src/fin_research/api/on_demand.py` | 신규(기존 작업) 정리 | 계정 수집과 임시 색인을 함수로 분리, `scripts` 지연 import |
| `src/fin_research/api/company_resolver.py` | 신규 | 기업·연도·기준·공시 확정, 되묻기 |
| `src/fin_research/api/data_source.py` | 신규 | 적재 확인, 데이터와 검색 도구 준비 |
| `src/fin_research/api/research_service.py` | 신규 | 전체 실행, 상태 판정, 정리 |
| `src/fin_research/api/research_routes.py` | 신규 | API, 메모리 작업 관리 |
| `tests/test_research_service.py` | 신규 | 외부 호출 없는 단위 테스트 52개 |
| `tests/integration/test_research_e2e.py` | 신규 | 실제 외부 호출 테스트 13개 (평소 건너뜀) |

서비스 코드에는 `patch`·`mock`이 없다(단위 테스트로 확인). 기존 `test_on_demand_research.py`, `test_on_demand_verify.py`는 유지했다.

## 4. 테스트 방법과 결과

```powershell
# 단위 테스트 (외부 호출 없음)
.venv/Scripts/python.exe -X utf8 -m pytest -q

# E2E (OpenDART·Supabase·Chroma·OpenAI 호출, 비용 발생)
$env:RUN_E2E = "1"
.venv/Scripts/python.exe -X utf8 -m pytest tests/integration/test_research_e2e.py -v -s
```

- 단위 테스트: **600개 통과, 20개 건너뜀**(기존 7개 + E2E 13개). 작업 전에는 548개 통과, 7개 건너뜀이었다.
- E2E 13개: **12개 통과, 1개 XFAIL(서진시스템)**. 전체 실행·색인 5개는 한 번씩, 나머지 8개는 두 번 실행했다. 8개의 첫 실행은 이벤트 루프 오류(7장)와 테스트 결과 파일명 문제로 4개 실패·5개 오류였고, 수정 후 모두 통과했다.
- 조사 설정은 `budget.yaml` 기본값, 주장 추출만 `small`을 지정했다.
- 원본 결과: `data/runs/e2e/*.json` (Git 제외)

| E2E 시나리오 | 입력 | 결과 |
|---|---|---|
| 미적재 기업 (API) | CJ대한통운의 2025년 연결 기준 재무 상태를 분석해줘. | 통과, `partial_success` |
| 적재 기업 | 서진시스템의 2025년 연결 기준 수익성과 재무 상태를 분석해줘. | **XFAIL**, `partial_success`, 판정 0건 |
| 적재 기업 (별도) | 리노공업의 2025년 별도 기준 재무 상태를 분석해줘. | 통과, `partial_success` |
| 기업명 없음 | 2025년 연결 기준 재무 상태를 분석해줘. | 통과, 되묻기 |
| 없는 이름 | 에코프로머티리얼의 2025년 연결 … | 통과, 되묻기 |
| 동명 기업 | 에스엠의 2025년 연결 … | 통과, 후보 11개 |
| 연도 누락 | CJ대한통운의 연결 … | 통과, 되묻기 |
| 기준 누락 | CJ대한통운의 2025년 … | 통과, 되묻기 |
| 유사 이름 (식별만) | 에코프로머티의 2025년 별도 … | 통과, 01311408로 확정 |
| 공시 없음 | CJ대한통운의 2026년 연결 … | 통과, `disclosure_not_found` |
| 강제 예외 | CJ대한통운 색인·검색 후 예외 | 통과, 임시 폴더 삭제 |
| 공용 Chroma 실패 (준비·검색만) | 서진시스템 | 통과, 임시 색인 210청크, 사유 기록 |
| Supabase 실패 | 닫힌 포트로 연결 | 통과, `storage_unavailable` |

- 강제 예외, 저장소 실패 시나리오는 실패를 만들기 위해 서비스의 주입 인자로 함수 하나를 바꿨다. 나머지 경로는 실제 코드다.
- 공용 Chroma 실패 시나리오에서 조사·검증까지 실행하지는 않았다.
- 마지막 테스트 무렵 공용 Chroma 서버가 실제로 끊겼고, 실제 확인 함수도 `connection_failed`를 반환했다.

## 5. 기업별 결과와 품질

### 5-1. 단계별 실행 시간(초)

| 기업 | 식별 | 준비 | 조사·보고서 | 검증 | 정리 | 합계 |
|---|---|---|---|---|---|---|
| CJ대한통운 (미적재) | 2.8 | **628.3** | 50.2 | 18.5 | 0.0 | 약 700 |
| 서진시스템 (적재) | 3.6 | 0.6 | 48.8 | 62.5 | 0.0 | 약 116 |
| 리노공업 (적재, 별도) | 3.5 | 0.6 | 58.8 | 40.3 | 0.0 | 약 103 |

미적재 기업은 CPU에서 335청크를 임베딩하는 시간이 대부분이다. 강제 예외 테스트의 같은 준비 단계는 510.1초였다.

### 5-2. 데이터 처리

| 기업 | mode | 정형데이터 | 검색 색인 |
|---|---|---|---|
| CJ대한통운 | `on_demand` | OpenDART 268행 | 임시 335청크 (문서 45개) |
| 서진시스템 | `loaded` | Supabase 355행 | 공용 182청크 (로컬 컬렉션은 0청크) |
| 리노공업 | `loaded` | Supabase 93행 | 공용 135청크 |

정형데이터 출처는 실제 사용한 곳으로 표시된다. (CJ: `OpenDART fnlttSinglAcntAll`, 나머지: `fin.dart_financial_accounts`)

### 5-3. 보고서 생성 품질

| 기업 | 글자 수 | 등록 근거 | 수익성 | 안정성 | 현금흐름 | 리스크 |
|---|---|---|---|---|---|---|
| CJ대한통운 | 3,232 | 112 | partial | partial | partial | draft |
| 서진시스템 | 3,145 | 111 | partial | draft | draft | **insufficient_evidence** |
| 리노공업 | 3,460 | 78 | partial | partial | draft | partial |

- 숫자 지표는 세 기업 모두 조회됐다. 부족한 부분은 변동 원인, 차입 조건, 감사의견·소송 같은 설명이다.
- 완료하지 못한 질문의 종료 사유(3개 기업 합계): `repeated_query` 13건, `no_next_query` 2건, `no_new_evidence` 2건, `max_rounds` 1건
- 서진시스템 결론은 숫자 사용 검사에 걸려 대체 문장이 쓰였다.
- 보고서 문장이 원문과 맞는지 사람이 검토하지는 않았다.

### 5-4. 주장 추출·검증 품질

| 기업 | 주장 | 일치 | 불일치 | 근거 없음 | 검증 불가 | 추출 호출 | 출력 토큰 |
|---|---|---|---|---|---|---|---|
| CJ대한통운 | 16 | 15 | 0 | 0 | 1 | 1 | 2,034 |
| 서진시스템 | **0** | 0 | 0 | 0 | 0 | 3 | 6,144 = 2,048 × 3 |
| 리노공업 | **1** | 1 | 0 | 0 | 0 | 3 | 4,223 |

- 검증 불가 1건은 검증 코드가 지원하지 않는 자기자본이익률(ROE)이다.
- **서진시스템 원인:** 보고서 전체가 한 묶음(기본 6,000자)으로 전달되고, `small`의 출력 한도 2,048토큰을 세 번 모두 채워 JSON이 잘렸다.
- **리노공업 원인:** 같은 이유로 두 번 잘린 것으로 보인다(합계에서 추정). 마지막 재시도가 매출액 1건만 반환했는데 **오류 없이 끝났다.**
- CJ대한통운은 한도까지 14토큰을 남기고 끝났다. 안정적인 성공으로 볼 수 없다.
### 5-5. 주장 누락 판정 (`claims_incomplete`)

추출 누락이 오류로 남지 않으므로, 보고서에 적힌 정형데이터 금액 중 주장으로 추출되지 않은 지표가 있으면 부분 성공으로 표시한다. 주장 추출 로직은 바꾸지 않았다.

- **E2E 이후에 추가했다.** 5-4의 실제 실행 결과에는 이 코드가 없으며, E2E를 다시 실행하지 않았다.
- 저장된 결과에 적용한 누락 지표: 서진시스템 12개, 리노공업 11개, CJ대한통운 0개. 세 기업의 상태는 그대로 `partial_success`다.
- 누락 판정은 단위 테스트로 확인했다. 이 코드로 서비스 전체 실행의 상태가 바뀌는 테스트는 없다.
- **감지하지 못하는 경우:** 보고서가 `1,234.6억원`처럼 반올림해 적은 금액, 비율 지표, 같은 지표가 다른 연도 값으로만 추출된 경우
- **잘못 감지할 수 있는 경우:** 금액을 문자열로 찾으므로 `999`가 `1,999` 안에 있어도 적힌 것으로 본다.

따라서 이 코드가 없다고 해서 추출 누락이 없다는 뜻은 아니다.

## 6. 저장소와 임시 리소스

| 항목 | 실행 전 | 실행 후 |
|---|---|---|
| Supabase `fin.dart_financial_accounts` | 30개 기업, 7,494행 | 동일 |
| 로컬 Chroma | 15개 컬렉션, 2,932청크 | 동일 |
| 공용 Chroma | 30개 컬렉션, 7,866청크 | E2E 13개 중 12개의 전후 비교에서 동일. 마지막 1개는 서버가 끊겨 **비교하지 못함** |
| `data/raw`, `data/processed` 파일 목록 | - | 동일 |
| 임시 폴더 `research_*` | 0개 | 0개 |

새로 생긴 파일은 테스트 결과 `data/runs/e2e/*.json`뿐이다. 서비스에는 Supabase와 영구 Chroma에 쓰는 코드가 없다. 단, 기업코드 캐시 `data/raw/dart/CORPCODE.xml`은 30일이 지나면 기존 함수가 갱신한다(테스트 중에는 갱신되지 않음).

기존 검색 코드는 로컬에 없는 기업을 조회할 때 `data/chroma_multi`에 빈 컬렉션을 만든다. 공용 색인을 쓸 때 빈 임시 경로를 전달해 이를 피했다.

## 7. 발견된 문제와 담당 영역

| 문제 | 담당 영역 | 처리 |
|---|---|---|
| 주장 추출 응답이 출력 한도에서 잘림 | 검증 | 수정하지 않음, 아래 제안 공유 |
| 재시도가 일부만 반환해도 정상 처리 | 검증 | 수정하지 않음, 서비스에서 부분 성공으로 표시 |
| 조·억 없는 금액에서 LLM이 값을 비우면 복구하지 못함 (`fill_value`) | 검증 | 수정하지 않음. 11.25 이전 테스트의 7건이 모두 이 경우 |
| ROE 검증 미지원 | 검증 | 수정하지 않음 |
| 검증 문구가 OpenDART 데이터도 "DB"로 표시 | 검증 | 근거의 출처 필드만 교체 |
| 변동 원인·리스크 설명 부족 | 조사·검색 | 수정하지 않음 |
| 결론에 숫자를 써서 대체 문장 사용 | 보고서 | 수정하지 않음 |
| `find_companies` 부분 일치 (HLB제약→HLB 등) | 공통 | OpenDART 정확 일치를 먼저 적용해 서비스에서 회피 |
| 로컬 색인(v1)과 공용 색인의 청크 수가 다름 | 데이터 | PC마다 검색 결과가 다를 수 있어 공유 |
| OpenAI 비동기 연결이 이벤트 루프 사이에서 재사용되어 실패 | 서빙 | **수정함.** 전용 루프 1개 사용 |

**별도 기준 검증:** 기존 코드는 기준이 없는 주장을 연결로 조회한다. 검증 그래프에 주입하는 `account_store`를 확장한 `RunAccountStore`로, 기준이 없는 주장만 이번 실행의 기준으로 조회해 **기존 코드 수정 없이 해결했다.** 연결로 명시된 주장은 별도 값과 비교하지 않는다. 실제 실행에서는 리노공업 1건에서만 확인했다.

**입력 크기 인터페이스 제안 (검증 담당자 협의):** 지금은 그래프 밖에서 묶음 크기를 줄일 방법이 없다. `claim_extract_node`가 `config["configurable"]`의 선택값 `claim_batch_max_chars`를 읽게 하면 약 3줄로 가능하고, 없으면 기존값을 쓴다. 이번 작업에서는 적용하지 않았다.

## 8. API 명세 (프론트엔드 연동용)

서버 실행 (프로젝트 루트, `vite.config.js`의 프록시가 8000 포트를 사용)

```powershell
.venv/Scripts/python.exe -X utf8 -m uvicorn fin_research.api.server:app --app-dir src --port 8000
```

### POST /api/research

| 필드 | 형식 | 설명 |
|---|---|---|
| `request` | 문자열 1~2,000자, 필수 | 기업명, 사업연도, 연결/별도를 포함한 요청 |
| `corp_code` | 숫자 8자리, 선택 | 동명 기업 후보를 선택했을 때 |

응답: `202`(접수), `422`(형식 오류), `429`(대기·실행 중 작업 10개 초과)

### GET /api/research/{id}

응답: `200`, `404`(없는 ID 또는 서버 재시작), `422`(UUID가 아님)

| `status` | 의미 | 화면 처리 |
|---|---|---|
| `queued` | 앞 작업을 기다리는 중 | 대기 표시 |
| `running` | 실행 중, `stage` 확인 | 진행 표시 |
| `success` | 전체 실행 완료, 품질 문제 없음 | 결과 표시 |
| `partial_success` | 보고서는 있으나 문제 있음 | 결과와 `quality.issues` 표시 |
| `failed` | 보고서 없음 | `error.message` 표시 |
| `needs_clarification` | 추가 정보 필요 | `question`과 `candidates` 표시 후 새로 POST |

- `stage`: `identify` → `prepare` → `research`(`research:brief` 등) → `verify` → `cleanup`
- 2~5초 간격 조회를 권장한다. 미적재 기업은 CPU에서 10분 이상 걸린다.
- 되묻기는 이전 요청을 이어받지 않는다. 빠진 조건을 포함한 **전체 요청을 다시 POST**한다.
- `missing`: `company`, `year`, `scope`

`result` 주요 필드

| 필드 | 내용 |
|---|---|
| `target` | 확정한 기업, 종목코드, 연도, 기준, 공시번호, 선택 사유 |
| `data` | `mode`, 정형데이터·색인 출처, 대체 사유 |
| `report` | 보고서 Markdown. `[S1]`은 `evidences[].evidence_id` |
| `findings` / `evidences` | 주제별 조사 결과와 한계 / 근거 원문과 위치 |
| `verification` | `claims`, `verdicts`, `summary`, `errors`. `claim_id`로 연결 |
| `execution` | 전체 실행 완료 여부, 단계별 상태와 시간 |
| `quality` | 결과 품질 문제 목록 |
| `error` / `cleanup` / `usage` | 오류 / 정리 결과 / LLM 사용량 |

- `verdicts[].label`: `match`, `mismatch`, `not_found`, `unverifiable`
- `claims[].page`는 PDF 쪽이 아니라 **보고서 섹션 순번**이다(1=제목, 2=요약, …).
- `quality.issues[].code`: `research_incomplete`, `research_errors`, `verification_not_completed`, `verification_errors`, `no_verdicts`, `claims_incomplete`, `claim_value_missing`, `verdict_not_found`, `verdict_unverifiable`
- `error.code`: `disclosure_not_found`, `storage_unavailable`, `context_mismatch`, `opendart_error`, `internal_error`
- 서비스가 처리하지 못한 예외로 끝나면(`failed`) `result`에 `error`만 있을 수 있다. 프론트엔드는 다른 필드가 없는 경우를 처리해야 한다.
- 불일치(`mismatch`)는 정상적인 검증 결과이므로 상태를 낮추지 않는다. 화면에서 `verification.summary.mismatch`를 별도로 표시해야 한다.

### 실제 요청과 응답

**① 조건 누락** (uvicorn 서버에서 실행)

```http
POST /api/research
{"request": "CJ대한통운의 재무 상태를 분석해줘."}

202
{"id": "a8eb7e73-7a64-4c02-b587-6c5377aa494e", "status": "queued"}
```

```http
GET /api/research/a8eb7e73-7a64-4c02-b587-6c5377aa494e   (실행 중)
{"id": "a8eb7e73-…", "status": "running", "stage": null, "result": null,
 "created_at": "2026-11-25T15:10:19.062295+00:00", "finished_at": null}
```

```http
GET /api/research/a8eb7e73-7a64-4c02-b587-6c5377aa494e   (완료, 일부 필드 생략)
{"id": "a8eb7e73-…", "status": "needs_clarification", "stage": "identify",
 "result": {
   "question": "어느 사업연도를 분석할까요? (예: 2025년) 연결 기준과 별도 기준 중 어느 기준으로 분석할까요?",
   "missing": ["year", "scope"], "candidates": [], "report": "", "data": null},
 "finished_at": "2026-11-25T15:10:28.941335+00:00"}
```

**② 미적재 기업** (E2E에서 같은 라우터로 실행, 일부 필드 생략)

```http
POST /api/research
{"request": "CJ대한통운의 2025년 연결 기준 재무 상태를 분석해줘."}
```

```json
{
  "status": "partial_success",
  "result": {
    "target": {"company": "CJ대한통운", "stock_code": "000120", "corp_code": "00113410", "fiscal_year": 2025,
               "statement_scope": "consolidated", "rcept_no": "20260316001417",
               "report_name": "사업보고서 (2025.12)", "selection_reason": "후보 1건"},
    "data": {"mode": "on_demand",
             "structured": {"status": "absent", "source": "OpenDART fnlttSinglAcntAll", "verify_row_count": 268},
             "search": {"source": "temporary_chroma", "fallback_reason": "not_loaded", "chunk_count": 335}},
    "report": "# CJ대한통운 재무 분석 보고서 (2025년)\n\n## 요약 …",
    "verification": {
      "claims": [{"id": "C1", "item": "매출액", "value": 12284652471280.0, "unit": "원", "year": 2025, "page": 2}],
      "verdicts": [{"claim_id": "C1", "label": "match", "db_value_won": 12284652471280.0,
                    "rcept_no": "20260316001417",
                    "evidence": [{"id": "C1.db", "kind": "db", "source_file": "OpenDART fnlttSinglAcntAll"}]}],
      "summary": {"match": 15, "mismatch": 0, "not_found": 0, "unverifiable": 1, "total": 16}},
    "execution": {"completed": true, "failed_stage": null},
    "quality": {"complete": false,
                "issues": [{"code": "research_incomplete",
                            "detail": {"profitability": "partial", "stability": "partial", "cashflow": "partial"}},
                           {"code": "verdict_unverifiable", "detail": ["C5"]}]},
    "cleanup": {"temp_root_removed": true, "error": "", "close_error": ""}
  }
}
```

**③ 동명 기업** (E2E 서비스 결과, 후보 11개 중 1개)

```json
{"status": "needs_clarification", "missing": ["company"],
 "question": "'에스엠'과(와) 이름이 같은 기업이 11개입니다. 고유번호(8자리)나 종목코드(6자리)를 함께 알려주세요.",
 "candidates": [{"corp_name": "에스엠", "corp_code": "00521178", "stock_code": "",
                 "ceo": "박병권,허광수", "industry": "도매 및 상품 중개업"}]}
```

## 9. 현재 제한 사항

- **메모리 상태:** 서버를 재시작하면 진행 중 작업과 결과가 사라진다. 워커가 여러 개면 서로의 작업을 조회하지 못하므로 **워커 1개**로 실행해야 한다. 끝난 작업은 최대 100개만 보관한다.
- 작업은 한 번에 하나씩 실행하며 취소 API가 없다. 서버가 강제 종료되면 임시 폴더가 남을 수 있다.
- 인증이 없고, 작업 ID를 알면 누구나 결과를 볼 수 있다.
- 주장 추출 기본 모델은 Qwen이다. Qwen 서버가 없으면 검증 단계가 실패하고 보고서만 있는 `partial_success`가 되도록 했으며, GPT로 바꾸는 코드는 없다. Qwen 기본 주소와 API 서버가 모두 **8000 포트**라 같은 PC에서는 `QWEN_BASE_URL`을 따로 지정해야 한다.
- 한 번에 기업 1개, 사업연도 1개만 처리한다. "최근 N년"은 되묻는다.
- 기업 약칭은 자동 선택하지 않는다. 정확한 기업명이나 코드가 필요하다.
- 정정공시가 여러 개면 기존 규칙대로 최신본을 쓴다. OpenDART 계정의 공시번호가 다르면 중단한다.
- 같은 요청도 실행마다 결과가 달라질 수 있다. 기업마다 한 번씩만 실행했다.
- 주장 누락 판정(`claims_incomplete`)은 모든 누락을 찾지 못한다(5-5).

## 10. 담당자별 개선 및 협의 사항

이번 작업에서는 기존 내부 로직을 수정하지 않았다.

| 담당 영역 | 개선·협의 사항 |
|---|---|
| 검증 (주장 추출) | 출력 한도에서 잘리는 문제, 재시도 시 주장 누락, 조·억 없는 금액 복구(`fill_value`) |
| 검증 | 입력 크기 선택 인자 협의(7장), ROE 지원 여부, 검증 문구의 "DB" 표현 |
| 검증·서빙 | Qwen 서버 준비 후 기본 설정 확인, 8000 포트 분리 |
| 조사·검색 | 변동 원인·리스크 설명 부족, `repeated_query`로 끝나는 질문 확인 |
| 보고서 | 결론에 숫자를 써서 대체 문장이 쓰이는 문제 |
| 데이터 | 로컬과 공용 Chroma의 청크 버전 통일 여부 |
| 공통 | `find_companies`의 부분 일치 (기존 CLI에서는 그대로 남아 있음) |
| 프론트엔드 | `client.js`의 `USE_MOCK` 해제, 조사 화면 연결, 되묻기·부분 성공 표시 |
| 서빙 | 작업 저장소·인증·취소, GPU 임베딩, 1-4의 미확인 항목 테스트 |
| 서빙 | 새 E2E로 기존 `test_on_demand_*.py`를 대체할지 결정 (현재는 유지) |

## 11. 이전 테스트 기록 (2026.11.25, patch 사용)

CJ대한통운의 생성과 검증을 따로 실행해 연결 가능성만 확인했다. 근거 48개, 판정 11건(일치 4, 검증 불가 7)이었다.
추출 묶음을 1,500자로 patch한 결과이며, 첫 묶음이 빠져 **매출액·영업이익·당기순이익은 검증되지 않았다.**
