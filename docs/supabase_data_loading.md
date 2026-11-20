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
