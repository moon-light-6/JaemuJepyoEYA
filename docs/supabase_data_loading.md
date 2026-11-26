# Supabase 재무 수치 적재 가이드

> **목적:** 담당 기업의 OpenDART 재무 수치를 JSON으로 생성한 뒤 Supabase의 `fin.dart_financial_accounts`에 적재합니다. XML 검색 청크는 **Supabase가 아닌 Chroma**에서 관리합니다.

## 0. 실행 준비

- 프로젝트 루트에서 **Windows CMD**와 `.venv`를 사용합니다. `scripts/ingestion/fetch_dart_accounts.py`, `scripts/storage/load_dart_accounts.py`와 XML 파이프라인의 `auto_resolve.py`가 있어야 합니다.
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
python scripts\ingestion\fetch_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO%
```

- **명령 설명:** OpenDART 재무계정 API에서 해당 기업의 수치를 조회하고 `data/processed/financial/` 아래 JSON으로 저장합니다.
- **확인:** `조회/저장 완료`, **계정 수**, **공시 접수번호**, 저장 경로를 확인합니다. 기존 파일은 자동으로 덮어쓰지 않습니다.

## 2. 적재 전 검증 (DB 변경 없음)

```cmd
python scripts\storage\load_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO%
```

- **명령 설명:** JSON의 기업·연도·연결/별도·접수번호·금액 형식을 검사하는 **DRY-RUN**입니다.
- **확인:** `확인 완료`와 `DRY-RUN: DB를 변경하지 않았습니다`가 출력되면 다음으로 진행합니다.

## 3. Supabase에 실제 적재

```cmd
python scripts\storage\load_dart_accounts.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --expected-rcept-no %RCEPT_NO% --apply
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
## 7. XML v2 ChromaDB 적재 및 확인 (2026-11-23)

> **목적:** 개선된 XML v2 청크가 로컬 ChromaDB에 정상적으로 적재되었는지 확인하고, 누락된 경우에만 기존 적재 함수를 사용합니다.
>
> Supabase의 재무 수치 적재와 ChromaDB의 XML 청크 적재는 별개의 작업입니다.

### 7-1. 담당 기업 정보 설정

Windows CMD에서 각자 담당 기업 정보로 설정합니다.

```cmd
set CORP_CODE=본인기업고유번호8자리
set YEAR=2025
set SCOPE=consolidated
set RCEPT_NO=공시접수번호14자리
```

`CORP_CODE`는 종목코드 6자리가 아닌 DART 기업고유번호 8자리입니다.

### 7-2. XML v2 청크 파일 확인

```cmd
dir data\processed\xml\dart\%CORP_CODE%\%YEAR%\%SCOPE%\%RCEPT_NO%_xml_chunks_v2.json
```

파일이 존재하면 다음 단계로 진행합니다.

파일이 없다면 해당 기업의 XML v2 청킹 작업이 완료됐는지, 다른 PC에 생성된 파일이 있는지 먼저 확인합니다.

### 7-3. ChromaDB 적재 상태 확인

```cmd
python -c "import hashlib,json,chromadb; from pathlib import Path; p=Path('data/processed/xml/dart/%CORP_CODE%/%YEAR%/%SCOPE%/%RCEPT_NO%_xml_chunks_v2.json'); d=Path('data/chroma_xml_v2')/hashlib.sha256(p.read_bytes()).hexdigest(); n='fin_xml_%CORP_CODE%_%YEAR%_%SCOPE%_%RCEPT_NO%_kure_v1'; assert (d/'chroma.sqlite3').is_file(), 'ChromaDB not found: '+str(d); data=json.loads(p.read_text(encoding='utf-8')); got=set(chromadb.PersistentClient(path=str(d)).get_collection(n).get(include=[])['ids']); ids={str(x['chunk_id']) for x in data}; print('XML:',len(data),'Chroma:',len(got),'Match:',len(got)==len(data) and ids==got)"
```

**결과 해석**

- `Match: True`: XML v2 JSON과 ChromaDB의 청크 개수 및 ID가 일치합니다.
- `Match: False`: 청크 수 또는 ID가 일치하지 않습니다.
- `ChromaDB not found`: 현재 PC에서 해당 청크 파일의 해시에 대응하는 Chroma 저장소를 찾지 못했습니다.

`Match: True`이면 재적재할 필요가 없습니다.

### 7-4. ChromaDB에 색인이 없을 경우에만 적재

기존 `xml_retrieval.py`에 구현된 `build_xml_chroma()` 함수를 재사용합니다. 새로운 스크립트를 만들 필요가 없습니다.

```cmd
python -c "import sys,hashlib,json,chromadb; from pathlib import Path; sys.path.insert(0,'src'); from fin_research.retrieval.xml_retrieval import build_xml_chroma; p=Path('data/processed/xml/dart/%CORP_CODE%/%YEAR%/%SCOPE%/%RCEPT_NO%_xml_chunks_v2.json'); d=Path('data/chroma_xml_v2')/hashlib.sha256(p.read_bytes()).hexdigest(); n='fin_xml_%CORP_CODE%_%YEAR%_%SCOPE%_%RCEPT_NO%_kure_v1'; count=build_xml_chroma(p,d,n); ids={str(x['chunk_id']) for x in json.loads(p.read_text(encoding='utf-8'))}; stored=set(chromadb.PersistentClient(path=str(d)).get_collection(n).get(include=[])['ids']); print('XML:',len(ids),'Chroma:',count,'Match:',ids==stored); assert ids==stored"
```

적재 후 `Match: True`가 출력되는지 확인합니다.

기존 인덱스와 입력 청크 ID가 다르다면 임의로 삭제하지 말고 원인을 먼저 확인합니다.

### 7-5. PC별 저장소 주의사항

ChromaDB는 각 PC의 로컬 디렉터리에 저장됩니다.

- `data/chroma_xml_v2/{SHA-256 해시}`: 개선된 XML v2 색인
- `data/chroma_multi`: 기존 검색 인덱스

따라서 노트북에 ChromaDB가 적재되어 있더라도 데스크톱에 자동으로 생성되는 것은 아닙니다.

Git에서 XML 청크 JSON을 가져온 경우에도 **실제로 검색할 PC에서 ChromaDB 적재 상태를 별도로 확인해야 합니다.**

### 7-6. 완료 확인 및 공유

각 기업별로 다음 항목을 확인합니다.

- Supabase `fin.dart_financial_accounts` 적재 완료 (`COMMIT`)
- XML v2 청크 JSON 존재
- ChromaDB 저장 개수 및 ID 일치 (`Match: True`)
- ChromaDB 적재·확인을 수행한 PC

**주의:** 이 절차는 데이터 적재 및 저장 상태 확인을 위한 것입니다. 검색기·LangGraph 연결이나 검색 정확도 검증은 별도 작업입니다.

### 평가 문항 관리 주의사항
신규 v2 문항 및 수정된 v1 정답은 evaluation/dataset/xml/에 저장됩니다.

기존 XML v2 평가 파이프라인은 검증 완료(verified)된 v1·v2 문항을 함께 불러와 평가할 수 있습니다.

단, Supabase 재무 수치 적재 및 ChromaDB 청크 색인 명령어로는 평가 문항 자체가 DB에 저장되지 않습니다. 문항을 추가했다는 이유만으로 재무 수치나 XML 청크를 다시 적재할 필요는 없습니다.