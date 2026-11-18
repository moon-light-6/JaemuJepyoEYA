# 재무제표 수치 DB 스키마 (PostgreSQL)

DART 사업보고서의 **연결재무제표 4종**(재무상태표·포괄손익계산서·자본변동표·현금흐름표)과 **연결 주석의 수치 표**를 저장하는 스키마다.
이 문서와 스키마는 **테이블 정의까지만** 다룬다. 데이터 적재(파서 연동)는 포함하지 않는다.

- 스키마 파일: `src/fin_research/storage/schema.sql`
- 필요 버전: PostgreSQL 12 이상, 확장 모듈 불필요
- 모든 객체는 `fin` 스키마 안에 만들어진다. 여러 번 실행해도 안전하다.

## 만드는 방법

```bash
# 이미 PostgreSQL이 있을 때
psql -d <DB명> -f src/fin_research/storage/schema.sql

# docker로 임시 DB를 띄울 때
docker run -d --name fin-pg -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
docker cp src/fin_research/storage/schema.sql fin-pg:/schema.sql
docker exec fin-pg psql -U postgres -f /schema.sql
```

조회할 때는 `SET search_path TO fin;` 을 먼저 하거나 `fin.테이블명`으로 쓴다.

## 구조

```
company ─< report ─< report_period (당기/전기/전전기)
              │
              ├─< fin_statement ─< fin_line_item ─< fin_value ─> report_period
              │      (BS/IS/CIS/SCE/CF)  (계정과목)     (금액)
              │
              ├─< note ─< note_table ─< note_cell ─> report_period (선택)
              │
              └─< source_chunk   (값/행/표의 chunk_id 가 가리키는 출처 원문)

std_account ─< std_account_alias      (fin_line_item.std_account_code 가 std_account 를 가리킴)
```

## 출처 추적과 표준 계정

**출처**: `fin_value`(와 `fin_line_item`, `note_*`)의 `chunk_id`는 `source_chunk`의 FK다. 값 하나에서 *보고서(`report.source_file`) → 쪽 번호(`pdf_page`) → 청크 원문(`source_chunk.text`)* 까지 SQL로 바로 따라간다. 존재하지 않는 `chunk_id`는 넣을 수 없다.

**표준 계정**: 회사마다 계정명이 다르다(예: 매출은 `매출액` / `수익(매출액)`, 영업이익은 `영업이익` / `영업이익(손실)`). `std_account_alias`가 공백 제거한 원문 이름을 표준 코드(`REVENUE`, `OPERATING_INCOME`, `ASSETS_TOTAL` …)로 묶고, 적재 시 `fin_line_item.std_account_code`가 자동으로 채워진다. 한 표 안에서 같은 코드는 첫 번째 행에만 붙는다(유니크 인덱스). 매핑이 없으면 NULL이다.
새 회사에서 이름이 다르면 별칭만 추가한다:

```sql
INSERT INTO fin.std_account_alias (statement_type, alias_norm, std_account_code)
VALUES ('CIS', '영업수익(손실)', 'REVENUE');
```

**비율 계산**: `v_fin_ratio`가 회사 × 보고서 × 연결/별도 × 기간별로 비율(%)을 계산한다(분모 0/값 없음은 NULL).

```sql
SELECT company_name, period_end, debt_ratio_pct, operating_margin_pct
FROM fin.v_fin_ratio WHERE period_role = 'current' AND fs_scope = 'consolidated';
```

비율 컬럼: `debt_ratio_pct`(부채비율), `current_ratio_pct`(유동비율), `equity_ratio_pct`(자기자본비율), `gross_margin_pct`(매출총이익률), `operating_margin_pct`(영업이익률), `net_margin_pct`(순이익률). ROE·ROA처럼 평균 잔액이 필요한 지표는 포함하지 않았다.

## 테이블

| 테이블 | 한 행의 의미 | 중복 방지(UNIQUE) |
|---|---|---|
| `company` | 기업 | `corp_code` (PK), `stock_code` |
| `report` | 사업보고서 1건 | `rcept_no` |
| `report_period` | 보고서의 기간 열 1개 | `(report_id, role)` |
| `fin_statement` | 재무제표 본문 1개 | `(report_id, fs_scope, statement_type)` |
| `fin_line_item` | 재무제표의 계정과목 행 | `(statement_id, line_no)` |
| `fin_value` | 계정과목 × 기간 금액 | `(line_id, period_id, column_key)` |
| `note` | 주석 1개 | `(report_id, fs_scope, note_no)` |
| `note_table` | 주석 안의 표 1개 | `(note_id, table_no)` |
| `note_cell` | 주석 표의 셀 1개 | `(note_table_id, row_idx, col_idx)` |
| `source_chunk` | 출처: 청크 원문 1개 (`chunks.json`의 청크) | `chunk_id` (PK) |
| `std_account` | 표준 계정 1개 (예: `ASSETS_TOTAL`) | `std_account_code` (PK) |
| `std_account_alias` | 원문 계정명 → 표준 계정 매핑 | `(statement_type, alias_norm)` |
| `v_fin_value` (뷰) | 금액 + 회사·기간·계정 정보를 합쳐 조회 | — |
| `v_fin_std_value` (뷰) | 표준 계정코드가 붙은 금액만 (회사 간 비교용) | — |
| `v_fin_ratio` (뷰) | 부채비율·유동비율·영업이익률 등 주요 비율(%) | — |

## 주요 컬럼

**company**: `corp_code`(DART 고유번호 8자리), `stock_code`(종목코드 6자리, 비상장 NULL), `name`.

**report**: `rcept_no`(접수번호 14자리, 형식 검사), `fiscal_year`, `source_file`(PDF 파일명), `rcept_date`.

**report_period**: `role`(`current`=당기, `prior`=전기, `prior2`=전전기), `term_no`(제N기), `period_start`/`period_end`.
재무상태표는 `period_end`가 기준일이다. 한 보고서의 모든 표가 이 행들을 공유한다.

**fin_statement**
- `statement_type`: `BS` 재무상태표 / `IS` 손익계산서 / `CIS` 포괄손익계산서 / `SCE` 자본변동표 / `CF` 현금흐름표.
- **손익계산서가 따로 없고 포괄손익계산서 하나로 합쳐진 회사는 `CIS` 행 하나만 저장한다.** 둘이 따로 있으면 `IS`와 `CIS`를 모두 저장할 수 있다.
- `fs_scope`: `consolidated`(연결, 기본) / `separate`(별도).
- `unit_label_raw`(원문 단위 표기), `unit_multiplier`(원=1, 천원=1000, 백만원=1000000).
- `pdf_page_start`/`pdf_page_end`: 표가 쪽을 넘어가는 경우를 위해 범위로 둔다.

**fin_line_item**: `line_no`(표 안의 행 순서, 쪽을 넘어도 이어서 매김), `account_name_raw`(PDF 원문 계정명), `account_name_norm`(정리된 이름), `std_account_code`(표준 계정코드, **NULL 허용**·나중에 채움), `note_refs`(원문 주석 참조, 예 `4,5,6,7`), `pdf_page`, `chunk_id`.

**fin_value** (금액)
| 컬럼 | 설명 |
|---|---|
| `amount_raw` | 원문 문자열 그대로 (`(1,234)`, `-` 등) |
| `amount_as_reported` | 표시된 숫자. 괄호는 음수로 변환, 단위 환산 전 |
| `amount` | 원 단위 환산값 = `amount_as_reported` × `unit_multiplier` |
| `value_status` | `ok` 값 있음 / `empty` 빈 칸 / `dash` '-' 표기. `empty`·`dash`면 금액 두 컬럼은 NULL (CHECK로 강제) |
| `column_key` | 자본변동표의 구성요소 열(자본금, 이익잉여금 등). 다른 표는 `''` |
| `pdf_page` | PDF 물리 쪽 번호 (`pages.json`의 `pdf_page`와 동일) |
| `chunk_id` | `chunks.json`의 `chunk_id`. 청크는 JSON/Chroma에 있으므로 FK는 걸지 않았다 |

자본변동표는 행이 기초·당기순이익·기말 등이고 열이 자본 구성요소다. `period_id`는 전기/당기 블록을 가리키고 구성요소 이름은 `column_key`에 넣는다.

**note / note_table / note_cell**: 주석은 표 모양이 제각각이라 셀 단위로 저장한다.
`note_no`는 `'4'`, `'4-1'`처럼 문자열이다. 셀은 `row_idx`/`col_idx`(위치), `row_label`/`col_label`(라벨), `text_raw`(셀 원문), 금액 컬럼(`fin_value`와 같은 규칙), `value_status`(`ok`/`empty`/`dash`/`text`)를 가진다. 열이 당기·전기에 해당하면 `period_id`로 연결한다.

## 인덱스

- `report(corp_code, fiscal_year)`: 회사·연도별 보고서 찾기
- `report_period(period_end)`: 기준일 검색
- `fin_line_item(account_name_norm)`, `fin_line_item(std_account_code)`: 계정과목 검색
- `fin_value(period_id)`, `fin_value(pdf_page)`, `fin_value(chunk_id)`: 기간별 조회, 출처 역추적
- `note_cell(row_label)`, `note_cell(period_id)`, `note_cell(pdf_page)`: 주석 표 조회

UNIQUE 제약은 인덱스를 함께 만든다.

## 조회 예

```sql
SET search_path TO fin;
-- 에코프로비엠 당기 영업이익(원 단위)과 출처 쪽
SELECT period_end, account_name_raw, amount, pdf_page, chunk_id
FROM v_fin_value
WHERE corp_code = '01160363' AND statement_type = 'CIS'
  AND account_name_norm LIKE '영업이익%' AND period_role = 'current';
```

## 데이터 적재 (`scripts/load_financials.py`)

`data/processed/dart/{corp_code}/{year}/{rcept_no}_pages.json`(+ `_chunks.json`)에서 연결재무제표 수치를 읽어 위 테이블에 넣는다.
파서는 `src/fin_research/ingestion/statement_parser.py`(DB 불필요), 적재는 `scripts/load_financials.py`.

```bash
pip install psycopg2-binary              # requirements.txt 에 추가됨
cp .env.example .env                     # DATABASE_URL 채우기 (.env 는 git 에 안 올라감)

python scripts/load_financials.py --corp-code 01160363 --dry-run   # DB 없이 파싱+검증만
python scripts/load_financials.py --corp-code 01160363             # 적재
```

- **합계 검증**: 재무상태표(자산 = 부채 + 자본), 현금흐름표 기말 현금 = 재무상태표 현금, 자본변동표 기말자본 = 재무상태표 자본총계를 확인한다. 하나라도 FAIL이면 적재하지 않는다(`--force`로만 예외).
- **재실행 안전**: 같은 보고서를 다시 넣으면 그 보고서의 표를 지우고 다시 넣는다. 중복이 생기지 않는다. 한 보고서는 한 트랜잭션이다.
- **청크 원문 적재**: 보고서의 청크 전체(주석 구간 포함)를 `source_chunk`에 넣는다. 값은 여기를 가리킨다.
- **표준 계정 자동 매핑**: 행을 넣은 뒤 `std_account_alias`로 코드를 채우고, 핵심 계정(자산총계·매출·영업이익 등)이 매핑되지 않으면 경고한다.
- **확인한 회사** (2025 사업보고서): 에코프로비엠, 에코프로, 알테오젠. 세 회사 모두 재무상태표·포괄손익계산서(`CIS`)·현금흐름표 합계 검증 통과, 표준 계정 21개 전부 매핑.
- **이미 스키마를 만든 DB**(예: Supabase)에는 `schema.sql`을 다시 실행하면 새 테이블·제약만 추가된다(기존 데이터 유지).

### 현재 한계 (적재 시 경고로 표시됨)

- **자본변동표(SCE)는 에코프로비엠 형식만 지원**한다. 에코프로, 알테오젠은 열 머리글이 달라 경고와 함께 건너뛴다(잘못된 값이 들어가지 않도록 통째로 제외).
- **주석 표(note_*)는 적재하지 않는다.** 테이블만 있고 파서가 없다.
- **정수가 아닌 값(주당순이익의 소수 등)은 저장하지 않고 경고**한다. 금액 컬럼이 bigint이기 때문이다. `403.0`처럼 정수값은 저장된다. 필요하면 컬럼을 numeric으로 바꿔야 한다.
- 주당순이익 행(`(원)` 표기)은 천원 단위 표라도 환산하지 않고 원 단위로 저장한다. 이 행만 `amount = amount_as_reported`이다.
- 삼성전자는 `data/processed`에 처리된 보고서가 없어 아직 시험하지 못했다.
- 표준 계정 별칭은 위 세 회사의 실제 계정명 기준이다. 다른 회사에서 이름이 다르면 별칭을 추가해야 하고, 누락 시 적재 로그에 경고가 나온다.

## 알아둘 점

- PDF 텍스트 추출값이라 표가 쪽을 넘어가거나 단위가 섞일 수 있다. 적재 시 표마다 `unit_multiplier`를 정확히 넣어야 `amount`가 맞는다.
- 값의 출처는 `pdf_page` + `chunk_id`로 추적한다. 청크 id는 FK가 아니므로 적재 시 `chunks.json`과 맞는지 확인한다.
- 별도 재무제표(`separate`)도 담을 수 있게 컬럼은 열어 두었지만, 이번 범위는 연결이다.
