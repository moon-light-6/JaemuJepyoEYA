# 다중 도구 조사: SQL·계산·문서 재검색 연결

## 실행 흐름

```text
질문 + 고정 기업·연도·연결/별도·접수번호
 → planner: 직접 지표 / 비율 / 설명 질문 분리
 → router: SQL → 계산 → 문서 검색 중 필요한 단계 선택
 → executor: 선택한 도구 실행
 → evaluate: 필수 자료와 설명 근거가 모두 있는지 검사
 → 숫자·계산은 코드로 작성하고 문서 답변의 출처 ID를 재연결
```

기존 문서 전용 `run_once`, `run_research_loop`는 유지한다. 새 흐름은 `--multi-tool`로 선택한다.
상위 보고서 그래프의 기존 Researcher 어댑터는 아직 문서 전용 루프를 사용한다.
이번 변경은 다중 도구 조사 함수와 독립 실행 명령, 통합 평가까지다.

## 파일과 책임

| 파일 | 역할 |
|---|---|
| `tools/financial_models.py` | 기업·실적 기간·공시·통화와 금액·계산 출처의 공통 자료형 |
| `tools/financial_account_tool.py` | 새 `fin.dart_financial_accounts`의 계정 의미·금액·중복·기준 확인 |
| `tools/calc_tool.py` | 동일 기준 입력으로 허용된 `ratios.py` 함수만 실행 |
| `research/loop/planner.py` | GPT가 질문을 구조화된 조사 요구로 변환 |
| `research/loop/router.py` | 계산 입력 지표를 자동 보충하고 실행 순서를 코드로 결정 |
| `research/loop/executor.py` | SQL·계산·기존 문서 재검색 루프 실행 |
| `research/loop/tool_graph.py` | LangGraph로 도구 순회·종료·결과 조립 |
| `tools/doc_search_tool.py`의 `XMLDocumentSearchTool` | XML 컬렉션 선택, 연도·범위·형식·선택적 섹션 필터, XPath 보존 |

## 숫자 조회 정책

이 모드의 숫자는 새로 적재한 `fin.dart_financial_accounts`를 읽는다. 기존 Text-to-SQL
도구(`run_sql.py`)의 자유 SELECT를 새 테이블에 그대로 허용하지 않는다.
GPT는 지표와 작업 종류만 고르고, 숫자는 고정된 매개변수 SQL로 조회한다.

- `corp_code`, `fiscal_year`, `fs_div`, `rcept_no`, 연간 `reprt_code='11011'`을 고정한다.
- 연결은 `CFS`, 별도는 `OFS`다. 현재 원화 연간 실적만 지원한다.
- 명확한 표준 계정 ID를 우선하고 계정명 대체는 제한된 정확한 별칭만 허용한다.
- 전체 당기순이익에 지배주주순이익을 대신 넣지 않는다.
- IS/CIS의 같은 값은 합산하지 않는다. 값이 다르면 보류한다.
- 통화·기준 불일치, 누락, 잘린 조회는 답을 확정하지 않는다.
- ROE는 같은 공시의 당기 순이익과 전기말·당기말 전체 자본을 사용한다.
- 숫자는 원본 DB 값이므로 임베딩 검색이나 OpenDART 재수집이 필요하지 않다.
- 숫자의 출처는 실제 관계명 `fin.dart_financial_accounts`, 접수번호·계정·행 식별자로 남긴다.
  존재하지 않는 PDF 페이지나 XML XPath를 임의로 붙이지 않는다.

계산은 부채비율, 유동비율, 전체 자본 기준 ROE, 영업이익률만 지원한다. 임의 Python 코드나
모델이 만든 공식을 실행하지 않는다. 0 이하 분모는 보류하고 손실에 따른 음수 이익률은 허용한다.

## 실제 GPT·DB 실행

프로젝트 루트의 활성 가상환경에서 CMD 한 줄로 실행한다.
`.env`의 OpenAI 키와 PostgreSQL 연결 정보가 필요하다. 조회 전용 URL을 설정했다면 우선 사용한다.

숫자 조회와 계산(임베딩 모델 불필요):

```bat
python -X utf8 scripts\run_loop.py --multi-tool --company "고영" --fiscal-year 2025 --statement-scope consolidated --rcept-no 20260312000804 --question "고영의 2025년 연결 매출액, 영업이익과 영업이익률을 알려줘." --out data\runs\multi_tool_goyoung.json
```

숫자와 설명을 함께 조사(XML 색인 필요):

```bat
python -X utf8 scripts\run_loop.py --multi-tool --company "고영" --fiscal-year 2025 --statement-scope consolidated --rcept-no 20260312000804 --question "고영의 2025년 연결 매출액과 회사가 설명한 매출 변화 원인을 알려줘." --max-rounds 3 --out data\runs\multi_tool_goyoung_reason.json
```

기본 설명 검색은 `data/chroma_multi`의 해당 XML 컬렉션을 사용한다. 별도 PDF 색인을 사용할 때만
`--source-format pdf`를 추가한다. XML 색인은 앞서 평가한 XML 파이프라인으로 준비한다.
다중 도구 조사는 OpenDART를 호출하거나 DB를 적재하지 않는다. GPT 호출 비용은 발생한다.

## 결과 읽기

- `plan`: GPT가 선택한 지표·비율·설명 질문.
- `route`, `tool_trace`: 실제 선택·실행한 도구와 오류·차단 여부.
- `facts`: 단위·실적 연도·출처가 붙은 원본 금액.
- `calculations`: 재현 가능한 입력 금액·공식 이름·계산 결과.
- `numbers`: 질문에서 요청한 최종 숫자만 반환. 계산에만 사용한 금액은 facts에 남는다.
- `document_research`: 기존 문서 루프의 검색·평가·종료 이력.
- `findings`: 숫자와 설명을 합친 답변. SQL S1과 문서 S1을 전역 ID로 재연결한다.
- `verification`: 입력 기준 검사·계산 실행과 원문 대조 검증을 구분한다.

실적 기간·계정 선택과 실제 모델의 계획은 추가 검수가 필요하다. 이 모드는 아직 공시 원문과
DB 값의 일치나 인과관계 자체를 자동 확정하지 않는다. 분기·반기, 여러 연도 비교, 임의 지표·공식은
이번 범위 밖이며, 명확한 지원 범위를 벗어나는 질문은 추가 확인하도록 프롬프트에 지시한다.
입력 기준과 질문 내용이 의미상 일치하는지에 대한 실제 모델 평가도 별도로 필요하다.

## 통합 모의 평가

```bat
python -X utf8 evaluation\run_eval.py --mode integration-mock --out data\runs\loop_qa_integration_report.json --observations-out data\runs\loop_qa_integration_observations.jsonl
```

생산 planner의 구조화 출력, router, executor, 계산 도구, LangGraph와 문서 재검색 루프를
실행하되 모델 응답·DB·검색 입력만 대체한다. 12개 가상 사례의 통과가 실제 기업 데이터나
GPT의 계획·검색 정확도 100%를 의미하지 않는다.
