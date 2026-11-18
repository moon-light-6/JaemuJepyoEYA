# 1주차 SQL 도구: PostgreSQL 연동

## 적용 범위

`db/financial-schema`의 `fin` 스키마를 사용한다. DuckDB는 사용하지 않는다.
재무 숫자와 출처 청크 원문은 PostgreSQL, 임베딩 유사도 검색은 기존 Chroma를 유지한다.
이 브랜치에 회원 테이블과 인증 연동은 없으므로 회원 데이터 통합까지 구현된 상태는 아니다.

- `storage/sql_store.py`: 단일 SELECT 구조 검사, 읽기 전용 연결, 최대 행 수·시간 제한, 연결 정리.
- `tools/sql_tool.py`: 질문과 스키마를 모델에 전달하고 SQL 또는 기준 확인 요청을 생성.
- `configs/prompts/research/sql.md`: 단위·당기/전기·연결/별도·출처 보존 규칙.
- `scripts/run_sql.py`: 모델 없는 직접 조회와 GPT 기반 Text-to-SQL 테스트 명령.
- `tools/ratios.py`: 부채비율·유동비율·평균자본 ROE·영업이익률의 공통 공식(기존 구현 유지).

1주차 완료 범위는 SQL 도구의 독립 실행이다. LangGraph의 planner/router/executor에
SQL 도구를 연결하고 계산 도구를 호출하는 작업은 2주차 범위로 남겨 둔다.

## 환경

프로젝트 루트에서 실행한다. 아래는 CMD에서도 그대로 쓰는 한 줄 명령이다.

```bat
python -m pip install -r requirements.txt
```

기존 `.env`에 `DATABASE_URL`을 설정한다(스키마·적재 코드와 동일).
조회 전용 계정이 있다면 `DATABASE_READONLY_URL`을 추가하면 우선 사용한다.
접속 문자열을 명령행 인자로 넘기거나 결과 JSON에 기록하지 않는다.
실제 `.env`와 로컬 변경된 `.env.example`을 커밋하지 않는다.

운영 시에는 DB 관리자가 `fin` 스키마 USAGE 및 아래 세 뷰에 대한 SELECT만 허용한
전용 로그인 계정을 마련하는 것이 좋다. 스키마 생성·적재 계정과 조회 계정을 분리한다.
허용 뷰: `fin.v_fin_value`, `fin.v_fin_std_value`, `fin.v_fin_ratio`.
이 도구는 DB 계정·권한을 자동 변경하지 않는다. 읽기 전용 트랜잭션의 공식 설명:
[PostgreSQL SET TRANSACTION](https://www.postgresql.org/docs/current/sql-set-transaction.html),
[Psycopg set_session](https://www.psycopg.org/docs/connection.html#connection.set_session).

## 실행

단계별 진행 로그는 기본으로 표시된다. `--quiet`로 숨길 수 있다.
재검색 루프를 포함한 사용법은 `docs/research_progress.md`를 참고한다.

DB와 모델을 호출하지 않고 SQL 구문 검사:

```bat
python -X utf8 scripts\run_sql.py --sql "SELECT company_name, amount, pdf_page, chunk_id FROM fin.v_fin_value WHERE value_status='ok' LIMIT 3" --plan-only
```

모델 없이 실제 PostgreSQL 조회:

```bat
python -X utf8 scripts\run_sql.py --sql "SELECT company_name, rcept_no, period_end, std_account_code, amount, pdf_page, chunk_id FROM fin.v_fin_std_value WHERE fs_scope='consolidated' AND period_role='current' AND std_account_code='REVENUE'" --out data\runs\sql_direct.json
```

GPT로 SQL 생성과 검사만 수행(모델 호출 비용 발생, DB 접속 없음):

```bat
python -X utf8 scripts\run_sql.py --question "에코프로비엠의 2025년 연결 매출액을 공시별로 구분해서 원 단위와 출처 페이지, 청크 ID로 조회해줘." --plan-only --out data\runs\sql_plan.json
```

GPT 생성 후 PostgreSQL 조회:

```bat
python -X utf8 scripts\run_sql.py --question "에코프로비엠의 2025년 연결 매출액을 공시별로 구분해서 원 단위와 출처 페이지, 청크 ID로 조회해줘." --out data\runs\sql_gpt.json
```

단위 테스트(API 호출·DB 접속 없음):

```bat
python -X utf8 -m unittest discover -s tests -p test_sql_tool.py -v
```

## 실행 정책과 한계

- SQL AST를 검사한다. 문자열 앞의 SELECT만 확인하는 방식이 아니다.
- 단일 SELECT와 지정된 뷰만 허용한다. DML/DDL, 여러 문장, 시스템 테이블,
  잠금, SELECT INTO, 임의 함수, WITH/UNION/윈도 함수 등은 거부한다.
- 접속은 읽기 전용 트랜잭션, 기본 쿼리 제한 10초, 잠금 대기 2초, 연결 제한 5초다.
- 기본 최대 100행(설정 최대 1000행)이며 한 행을 추가 확인해 `truncated`로 잘림을 표시한다.
- 정수는 정수, Decimal은 문자열로 JSON에 보존한다. 날짜는 ISO 문자열이다.
- 응답에 원 단위 `amount`가 있으면 표시용 `amount_display`를 추가한다.
  예: `amount: 2531576384113`, `amount_display: "약 2.532조 원"`.
  조·억·만 단위는 소수 셋째 자리까지 반올림해 표시한 근삿값이고 1만원 미만은 원 단위 그대로 표시한다.
  금액 누락은 표시값도 null이다. 화면에는 표시값을, 계산·검증에는 원본 amount를 사용한다.
- 기준이 불명확하면 모델이 `needs_clarification`으로 응답하도록 요청한다.
  생성 SQL이 보고서 연도만 필터하고 실적 기간을 제한하지 않으면 차단한다.
  구문·기간 조건 검사 실패 시 모델에 한 번 수정 요청하고, 재실패하면 실행하지 않는다.
  구문 검사는 SQL의 의미까지 보장하지 않는다. 회사·기간·범위 선택의 정확도는 별도 평가가 필요하다.
- 결과가 0행이면 미적재·미매핑일 수 있다. 값이 0이거나 해당 사실이 없다고 단정하지 않는다.
- 기존 적재기는 연결 중심이며 주석 표·일부 SCE·소수 값 적재 등에 한계가 있다.
  상세 범위는 `docs/db_schema.md`를 따른다.
- `v_fin_ratio`는 ROE가 없고 소수 둘째 자리 반올림, 0 분모 제외 정책이다.
  `ratios.py`는 표시 반올림을 하지 않고 0 이하 분모를 거부한다. 검증용 계산은 원본 금액과
  `ratios.py`를 사용하고, 두 결과를 같은 정책의 정답처럼 취급하지 않는다.
- SQL 실행 성공은 보고서 주장의 일치 판정이 아니다. 공식 수치와 주장 비교는 후속 검증 작업이다.
