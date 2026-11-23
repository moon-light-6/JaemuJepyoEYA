# XML 평가 정답표(v1·v2) → Supabase 적재 가이드

> **대상:** 각자 담당 기업의 XML 검색 평가 문항을 검증한 팀원  
> **실행 환경:** 프로젝트 루트의 **Windows CMD** + 가상환경(`.venv`)  
> **목적:** 최종 검증된 질문·정답·XML 근거를 Supabase의 `fin.xml_eval_ground_truth_v2` 테이블에 저장

## 먼저 알아둘 점

- 이 작업은 **검색 평가 정답표(Ground Truth)** 적재입니다. 기존 재무 수치 테이블 `fin.dart_financial_accounts` 적재나 ChromaDB 청크 색인과는 **별개**입니다.
- 사용하는 스크립트: `scripts/load_xml_eval_ground_truth.py`
- 입력 파일: `evaluation/dataset/xml/{기업고유번호}/{사업연도}/{연결·별도}/{접수번호}_retrieval.json`(v1), `..._retrieval_v2.json`(v2)
- **v1 파일은 필수**, **v2 파일은 있으면 함께 적재**합니다.
- `annotation_status = verified`인 문항만 DB에 넣습니다. `excluded`/`rejected`는 적재하지 않습니다. 그 외 미검증 상태가 남아 있으면 실제 적재를 중단합니다.
- **중요: `--apply`는 추가만 하는 명령이 아닙니다.** 지정한 **기업 + 사업연도 + 연결/별도 + 접수번호**의 기존 정답표 행을 **삭제하고**, 현재 검증된 v1·v2 문항 전체를 **같은 트랜잭션에서 다시 저장**합니다. 따라서 최종 JSON이 확정된 후 실행하세요. **다른 공시의 데이터는 삭제 대상이 아닙니다.**
- Supabase 테이블은 팀에서 **한 번만 생성**합니다. 개인별로 테이블 생성 SQL을 반복 실행하지 마세요.

## 1. 실행 준비

프로젝트 루트에서 CMD를 열고 `.venv`를 활성화합니다. (`.venv`가 이미 표시되면 활성화 명령은 생략.)

```cmd
.venv\Scripts\activate
```

현재 브랜치와 파일 존재 여부를 확인합니다.

```cmd
git branch --show-current
git status
dir scripts\load_xml_eval_ground_truth.py
```

- **확인:** `scripts\load_xml_eval_ground_truth.py`가 출력되어야 합니다. 없다면 **담당자가 해당 코드를 Git에 반영한 뒤** 최신 브랜치를 받아야 합니다. 다른 적재 스크립트로 대체하지 마세요.
- `.env`에 **실제 적재 대상 Supabase 프로젝트의 `DATABASE_URL`**이 있어야 합니다. 팀에서 공유받은 **접속 설정 방법**을 사용하고, 비밀번호나 `.env` 파일은 Git/메신저에 올리지 마세요.
- 아직 테이블이 생성되지 않았다면 DB 담당자에게 요청합니다. 테이블명: `fin.xml_eval_ground_truth_v2`.
- 실제 적재 시 패키지가 없다는 오류가 나오면:

```cmd
pip install psycopg2-binary python-dotenv
```

## 2. 본인 기업 정보 설정

아래는 **CJ ENM 예시**입니다. `set`의 값을 **본인 담당 공시 정보**로 바꾸세요.

```cmd
set "CORP_CODE=00265324"
set "YEAR=2025"
set "SCOPE=consolidated"
set "RCEPT_NO=20260318001469"
```

| 변수 | 입력 내용 | 주의사항 |
|---|---|---|
| `CORP_CODE` | DART **기업고유번호 8자리** | 주식 종목코드 6자리가 아님 |
| `YEAR` | 사업연도 | 예: `2025` |
| `SCOPE` | `consolidated` 또는 `separate` | 연결 = `consolidated`, 별도 = `separate` |
| `RCEPT_NO` | 공시 **접수번호 14자리** | 해당 기업·사업연도·연결/별도와 맞는지 확인 |

입력 값 확인:

```cmd
echo %CORP_CODE% / %YEAR% / %SCOPE% / %RCEPT_NO%
```

## 3. 정답 JSON 파일 확인

```cmd
dir evaluation\dataset\xml\%CORP_CODE%\%YEAR%\%SCOPE%\%RCEPT_NO%_retrieval.json
dir evaluation\dataset\xml\%CORP_CODE%\%YEAR%\%SCOPE%\%RCEPT_NO%_retrieval_v2.json
```

- **v1 파일:** 반드시 있어야 합니다.
- **v2 파일:** 추가 문항을 만든 기업만 있으면 됩니다. **v2가 없는 기업은 v1만 적재하는 것이 정상**입니다.
- JSON의 문항 상태와 정답이 **최종 검증·저장된 버전**인지 먼저 확인하세요. **초안(draft) 상태로 진행하지 마세요.**

## 4. DRY-RUN: 적재 전 검증 (DB 변경 없음)

```cmd
python scripts\load_xml_eval_ground_truth.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --rcept-no %RCEPT_NO%
```

예시 출력(CJ ENM에서 실제 확인한 결과):

```text
대상: 00265324 / 2025 / consolidated / 20260318001469
파일: v1=20260318001469_retrieval.json, v2=20260318001469_retrieval_v2.json
검증 상태: v1/verified=5, v2/verified=11
적재 대상: 16개 (v1 5 / v2 11)
금액 문자열 양끝 공백 정리: 2개 (source_row는 원본 유지)
DRY-RUN 완료: DB는 변경하지 않았습니다.
```

**여기서 확인할 것**

1. `대상`의 기업고유번호·연도·기준·접수번호가 정확한가?
2. `파일`에 의도한 v1·v2 정답표가 잡혔는가? (v2가 없으면 v1만 표시)
3. `검증 상태`가 최종 검수 결과와 일치하는가?
4. `적재 대상` 건수가 **본인이 확인한 verified 문항 수**와 같은가?
5. 마지막에 **`DRY-RUN 완료: DB는 변경하지 않았습니다.`**가 표시되는가?

> **주의:** DRY-RUN은 JSON의 형식·중복 문항·금액·근거 등 적재 조건을 검증하지만, **Supabase에 접속하지는 않습니다.** DB 연결·권한 문제는 실제 적재 시 별도 확인해야 합니다.
>
> `verified가 아니어서 제외한 문항`이 표시되면 상태와 제외 사유를 확인하세요. `excluded`·`rejected`는 제외할 수 있지만, 그 외 미검증 상태가 있으면 `--apply`가 중단됩니다.

## 5. 실제 Supabase 적재 (`--apply`)

**4단계의 대상과 문항 수가 정확할 때만** 실행합니다.

```cmd
python scripts\load_xml_eval_ground_truth.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --rcept-no %RCEPT_NO% --apply
```

정상 완료 예시:

```text
Supabase 호스트: aws-0-ap-northeast-2.pooler.supabase.com
COMMIT 완료: 동일 공시 0건 → 16건
```

**여기서 확인할 것**

- **`COMMIT 완료`**가 있어야 트랜잭션 완료를 확인할 수 있습니다.
- `→` 오른쪽 건수가 **DRY-RUN의 `적재 대상` 건수와 같아야** 합니다.
- `0건 → N건`: 해당 공시가 처음 적재된 경우의 예시입니다.
- `기존 M건 → N건`: 이미 있던 **동일 공시 정답표를 현재 파일 내용으로 교체**한 것입니다. 의도하지 않은 재실행이면 그 이유를 확인하세요.
- 오류로 종료되면 완료로 보고하지 마세요. **비밀번호가 포함된 전체 DB URL은 공유하지 마세요.**

## 6. Supabase SQL Editor에서 결과 확인

Supabase Dashboard → **SQL Editor**에서 아래 쿼리를 실행합니다. **아래 예시의 기업 정보 4곳을 본인 값으로 바꾸세요.**

```sql
SELECT
    dataset_version,
    COUNT(*) AS question_count
FROM fin.xml_eval_ground_truth_v2
WHERE corp_code = '00265324'
  AND fiscal_year = 2025
  AND statement_scope = 'consolidated'
  AND rcept_no = '20260318001469'
GROUP BY dataset_version
ORDER BY dataset_version;
```

CJ ENM의 실제 확인 결과:

| dataset_version | question_count |
|---|---:|
| `v1` | 5 |
| `v2` | 11 |

**확인:** `v1`·`v2` 각각의 건수가 DRY-RUN 출력과 같아야 합니다. **v2가 없는 기업은 `v1` 행만 나오는 것이 정상**입니다.

몇 개 문항의 상세값까지 확인하려면:

```sql
SELECT
    question_id,
    dataset_version,
    question,
    expected_value,
    account_nm_raw,
    account_nm_norm,
    gold_sources
FROM fin.xml_eval_ground_truth_v2
WHERE corp_code = '00265324'
  AND fiscal_year = 2025
  AND statement_scope = 'consolidated'
  AND rcept_no = '20260318001469'
ORDER BY question_id;
```

- `expected_value`: 정답 금액(숫자 문자열 또는 연도별 비교값)
- `account_nm_raw`: 원래 계정명
- `account_nm_norm`: **계정명 공백 제거** 형태
- `gold_sources`: 정답 근거 XML 파일·XPath

**주의:** SQL 건수 확인은 **적재 및 건수 일치**를 확인하는 절차입니다. 문항의 숫자와 XML 원문 근거가 맞는지는 팀원이 완료한 **정답 검수 결과**와 대조해야 합니다.

### 전체 기업 적재 현황 보기 (선택)

```sql
SELECT
    corp_code,
    fiscal_year,
    statement_scope,
    rcept_no,
    COUNT(*) FILTER (WHERE dataset_version = 'v1') AS v1,
    COUNT(*) FILTER (WHERE dataset_version = 'v2') AS v2,
    COUNT(*) AS total
FROM fin.xml_eval_ground_truth_v2
GROUP BY corp_code, fiscal_year, statement_scope, rcept_no
ORDER BY corp_code, fiscal_year, statement_scope, rcept_no;
```

## 7. 오류가 나면 확인할 것

| 증상/메시지 | 점검할 내용 |
|---|---|
| `v1 정답 파일이 없습니다` | 3단계 경로와 기업고유번호·접수번호 확인. v1은 필수 |
| v2 문항이 출력되지 않음 | `_retrieval_v2.json` 파일 존재 여부 확인. 없으면 v1만 적재 |
| `검증 실패:` | 문항 ID 중복, 질문 중복, 금액 형식, `gold_sources`, 필수 필드 확인 |
| `미검증 문항이 남아 있어 --apply 중단` | `annotation_status` 확인. 최종 검수 없이 상태를 임의 변경하지 말 것 |
| `필수 패키지 없음` | 1단계의 `pip install psycopg2-binary python-dotenv` 실행 |
| `.env의 DATABASE_URL이 없습니다` | 프로젝트 루트 `.env`의 DB 설정 확인. **비밀번호 공유 금지** |
| `테이블이 없습니다: fin.xml_eval_ground_truth_v2` | DB 담당자에게 테이블 생성 여부와 현재 연결된 프로젝트 확인 |
| DB 연결·권한 관련 오류 | `DATABASE_URL`의 Supabase 프로젝트/접속 권한, 테이블 소유자·RLS 설정을 DB 담당자에게 확인 |
| SQL 조회 건수와 DRY-RUN 건수가 다름 | 동일한 기업·사업연도·기준·접수번호로 조회했는지 확인. 재실행하기 전 로그 공유 |

## 8. 완료 후 팀 공유 양식

아래 형식으로 공유하면 됩니다.

```text
[XML 정답표 Supabase 적재 완료]
기업명:
corp_code:
사업연도 / 연결·별도:
rcept_no:
DRY-RUN: v1 __건 / v2 __건 / 합계 __건
실제 적재: COMMIT 완료 / __건
Supabase SQL 조회: v1 __건 / v2 __건 / 합계 __건
특이사항: 없음 (또는 상세 내용)
```

---

### 작업 범위 구분

| 작업 | 저장 위치 | 이 문서에서 처리? |
|---|---|---|
| 최종 검증한 XML 평가 정답 문항(v1·v2) | `fin.xml_eval_ground_truth_v2` | **예** |
| OpenDART 원천 재무계정 | `fin.dart_financial_accounts` | 아니요 |
| 검색용 XML 청크 및 임베딩 | 로컬 `data/chroma_xml_v2/...` | 아니요 |

**관련 문서:** `docs/supabase_data_loading.md`(재무계정·Chroma 적재 안내). 이번 파일은 **평가 정답표 Supabase 적재만** 다룹니다.

> **팀 배포 전 확인:** `scripts/load_xml_eval_ground_truth.py`와 테이블 생성 SQL이 팀 공용 브랜치에 반영되어 있고, DB 테이블 `fin.xml_eval_ground_truth_v2`가 이미 생성되어 있어야 팀원이 이 절차를 그대로 실행할 수 있습니다.
