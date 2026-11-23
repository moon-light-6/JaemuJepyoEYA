# Supabase 재무 수치 적재 가이드

> **목적:** 담당 기업의 OpenDART 재무 수치를 JSON으로 생성한 뒤 Supabase의 `fin.dart_financial_accounts`에 적재합니다. XML 검색 청크는 **Supabase가 아닌 Chroma**에서 관리합니다.

## 0. 실행 준비

- 프로젝트 루트에서 **Windows CMD**와 `.venv`를 사용합니다. `scripts/fetch_dart_accounts.py`, `scripts/load_dart_accounts.py`와 XML 파이프라인의 `auto_resolve.py`가 있어야 합니다.
- `.env`에는 `DART_API_KEY`, Supabase의 `DATABASE_URL`이 설정되어 있어야 하며, **`.env`는 Git에 커밋하지 않습니다.**

```cmd
set CORP_CODE=01095722
set YEAR=2025
set SCOPE=consolidated
set RCEPT_NO=20260317000641
```

- **명령 설명:** 위 값은 *심텍 예시*이며, 각자 담당 기업의 **DART 고유번호(8자리)**, 사업연도, 기준, **공시 접수번호(14자리)**로 변경합니다.
- **확인:** `consolidated`는 연결, `separate`는 별도입니다. 사업연도와 접수번호가 같은 공시를 가리키는지 확인합니다.

## 1. 수치용 JSON 생성

```cmd
python scripts\fetch_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO%
```

- **명령 설명:** OpenDART 재무계정 API에서 해당 기업의 수치를 조회하고 `data/processed/financial/` 아래 JSON으로 저장합니다.
- **확인:** `조회/저장 완료`, **계정 수**, **공시 접수번호**, 저장 경로를 확인합니다. 기존 파일은 자동으로 덮어쓰지 않습니다.

## 2. 적재 전 검증 (DB 변경 없음)

```cmd
python scripts\load_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO%
```

- **명령 설명:** JSON의 기업·연도·연결/별도·접수번호·금액 형식을 검사하는 **DRY-RUN**입니다.
- **확인:** `확인 완료`와 `DRY-RUN: DB를 변경하지 않았습니다`가 출력되면 다음으로 진행합니다.

## 3. Supabase에 실제 적재

```cmd
python scripts\load_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO% --apply
```

- **명령 설명:** 검증한 수치 데이터를 Supabase의 `fin.dart_financial_accounts` 테이블에 INSERT/UPSERT합니다.
- **확인:** `적재 완료 ... (트랜잭션 COMMIT)`과 **적재 건수**를 확인하고, 1단계 계정 수와 비교합니다.

## 4. Supabase 화면에서 확인

- **확인 경로:** Supabase Dashboard → **Table Editor** → 스키마 **`fin`** → **`dart_financial_accounts`**.
- **확인:** 담당 `corp_code`·`fiscal_year`·`fs_div`·`rcept_no`로 필터링해 행 수와 주요 금액을 원본 공시와 대조합니다.

## 5. Chroma 상태 확인 (별도 작업)

```cmd
python -c "import chromadb; c=chromadb.PersistentClient(path='data/chroma_multi'); n='fin_xml_%CORP_CODE%_%YEAR%_%SCOPE%_%RCEPT_NO%_kure_v1'; print('Chroma 컬렉션:',n); print('저장된 청크:',c.get_collection(n).count())"
```

- **명령 설명:** *현재 PC*의 XML Chroma 컬렉션에 저장된 청크 수를 **조회만** 합니다. Supabase 적재와는 별개입니다.
- **확인:** 해당 기업의 `xml_chunks.json` 청크 수와 일치하는지 확인합니다. 컬렉션이 없으면 색인 여부와 로컬 저장 경로를 확인합니다.

## 완료 후 공유

`기업명 / corp_code / 사업연도 / 연결·별도 / rcept_no / Supabase 적재 건수 / Chroma 청크 수 / 주요 수치 대조 결과`

> **주의:** OpenDART API 조회 결과와 XML 공시 접수번호가 다르면 임의로 맞추지 말고 확인합니다. Supabase 데이터는 팀원이 공유할 수 있지만 `data/chroma_multi`는 기본적으로 **각 PC의 로컬 저장소**이므로 팀 공용 검색 환경은 별도로 구성해야 합니다.


# 261123

> **목적:** 기존 OpenDART 수치 재적재 절차를 팀 공용으로 정리하고, 기존 평가 문항(v1) 수정 및 추가 평가 문항(v2)과의 관계를 명확히 합니다. 아래 명령어는 기존 0~5절의 **Windows CMD 형식**을 그대로 따릅니다.
>
> **주의:** 문항을 추가하거나 정답을 수정했다고 해서 OpenDART 원본 재무계정 데이터가 자동으로 바뀌는 것은 아닙니다. 현재 `load_dart_accounts.py`는 `fin.dart_financial_accounts`의 수치만 적재하며, v1/v2 평가 문항 JSON이나 검색 평가 결과를 Supabase에 적재하지 않습니다.

## 1. 각자 담당 기업 정보 설정

프로젝트 루트의 Windows CMD에서 기존 문서 0절과 같은 방식으로 설정합니다. `...`는 실제 값으로 변경하세요. 한 기업을 완료한 뒤 다음 기업으로 이동할 때는 다시 설정합니다.

```cmd
set CORP_CODE=본인기업의8자리DART고유번호
set YEAR=2025
set SCOPE=consolidated
set RCEPT_NO=해당공시의14자리접수번호
```

- `CORP_CODE`는 종목코드(6자리)가 아닌 DART 기업고유번호(8자리)입니다. 앞의 `0`도 포함합니다.
- `SCOPE`는 연결 `consolidated`, 별도 `separate`입니다.
- `.env`에 `DART_API_KEY`와 `DATABASE_URL`이 설정되어 있어야 합니다.
- 팀원별 수집 대상 공시와 평가셋의 `rcept_no`를 일치시킵니다.

## 2. OpenDART 수치 JSON 수집 또는 재수집

**기존 JSON이 없는 경우: 기존 문서의 명령어 그대로 실행**

```cmd
python scripts\fetch_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO%
```

**기존 JSON이 있고 수치를 다시 조회해야 하는 경우에만:** 기존 명령어 맨 끝에 `--overwrite`를 추가합니다.

```cmd
python scripts\fetch_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO% --overwrite
```

- `--overwrite`는 `data/processed/financial/`의 **로컬 수치 JSON을 덮어쓰는 옵션**입니다. Supabase 초기화 옵션이 아닙니다. 기존 파일 보존이 필요하면 먼저 백업하세요.
- API 응답 접수번호와 `%RCEPT_NO%`가 다르면 수집을 중단합니다. 임의로 번호를 변경하지 마세요.
- 문항 추가가 유일한 변경 사항이라면 이 수치 JSON 재조회는 필수 단계가 아닙니다.

## 3. 적재 전 검증: DRY-RUN (기존 명령어와 동일)

```cmd
python scripts\load_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO%
```

`확인 완료`와 `DRY-RUN: DB를 변경하지 않았습니다`를 확인합니다. 이 단계에서는 DB를 수정하지 않습니다.

## 4. Supabase INSERT / UPSERT (기존 명령어와 동일)

```cmd
python scripts\load_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO% --apply
```

- `fin.dart_financial_accounts` 테이블에 적재합니다. 동일 `(rcept_no, fs_div, record_index)`의 행은 UPDATE, 없으면 INSERT합니다.
- 기존 동일 공시의 행 수가 새 JSON의 계정 수보다 많으면 스크립트가 오류를 내고 중단합니다. 임의 삭제·강제 초기화하지 마세요.
- `적재 완료: fin.dart_financial_accounts / N건 (트랜잭션 COMMIT)` 메시지와 계정 수를 확인합니다.

## 5. Supabase 적재 결과 확인 (팀 공통)

Supabase SQL Editor에서 적재된 전체 기업을 확인하려면 다음 SQL을 실행합니다.

```sql
SELECT corp_code, fiscal_year, fs_div, rcept_no, COUNT(*) AS account_rows
FROM fin.dart_financial_accounts
GROUP BY corp_code, fiscal_year, fs_div, rcept_no
ORDER BY corp_code, fiscal_year, fs_div, rcept_no;
```

담당 기업의 `account_rows`와 수집·적재 로그의 계정 수를 대조합니다.

## 6. 수정된 v1 / 추가된 v2 평가 문항 확인 (수치 적재와 별개)

```cmd
dir evaluation\dataset\xml\%CORP_CODE%\%YEAR%\%SCOPE%\%RCEPT_NO%_retrieval*.json
```

- `{rcept_no}_retrieval.json`: 기존 v1 평가 문항과 수정된 정답.
- `{rcept_no}_retrieval_v2.json`: 추가 생성·검증한 v2 평가 문항.
- 각 파일의 `id`, `question`, `expected_value`, `unit`, `gold_sources`, `annotation_status`를 확인하고, `verified` 여부를 점검합니다.
- **현재 `scripts/load_dart_accounts.py`로는 이 문항을 Supabase에 넣을 수 없습니다.** 실제 DB에 문항 수정·추가를 반영하려면 별도 테이블/UPSERT 스크립트가 필요하며, 확인된 현행 적재 명령어는 없습니다.
- XML 청크와 Chroma 인덱스, `evaluation/results/xml/`의 기존/개선 청킹 평가 결과 또한 위 수치 적재 명령어로는 저장되지 않습니다.

> **팀 공통 완료 기록:** 기업명 / DART 고유번호 / 연도 / 연결·별도 / 접수번호 / 수치 JSON 계정 수 / DRY-RUN / Supabase 적재 건수 / v1·v2 `verified` 문항 수.
