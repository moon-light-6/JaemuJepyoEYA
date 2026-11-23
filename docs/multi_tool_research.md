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
실행하되 모델 응답·DB·검색 입력만 대체한다. 13개 가상 사례의 통과가 실제 기업 데이터나
GPT의 계획·검색 정확도 100%를 의미하지 않는다.

## XML 재검색 및 v2 청크 사용

첫 검색은 벡터 검색이다. 두 번째부터는 재무제표 주석을 우선 검색하고 같은 필터의 BM25 결과와 RRF로 결합한다. 후보 안에서는 이전에 확보하지 않은 청크를 우선한다. 섹션을 명시했다면 필터를 완화하지 않는다. 키워드 검색은 최대 5천 청크까지만 준비하며, 초과하면 벡터 검색만 사용한다. 문서가 색인 범위에 없으면 이 방식도 근거를 만들 수 없다.

`document_research.history[].search_info`에서 전략, 컬렉션, 필터와 청크 버전을 확인한다. XML 로그에는 페이지 대신 XPath가 표시된다.

새 브랜치의 v2 청크로 이미 색인했다면 다음 명령을 사용한다.

```bat
python -X utf8 scripts\run_loop.py --multi-tool --xml-version 2 --company "고영" --fiscal-year 2025 --statement-scope consolidated --rcept-no 20260312000804 --question "고영의 2025년 연결 매출액과 회사가 설명한 매출 변화 원인을 알려줘." --max-rounds 3 --out data\runs\multi_tool_goyoung_reason_v2.json
```

v2는 기본 청크 JSON의 SHA-256으로 `data/chroma_xml_v2/<해시>` 저장소를 찾는다. `--persist-directory`는 해시 폴더의 상위 경로다. 색인이나 청크가 없으면 오류로 종료하며 자동 다운로드·재색인·v1 전환은 하지 않는다. 기본 청크 JSON 경로를 사용하는 v2 파이프라인과 연동한다. 사용자 지정 `--chunks-path`로 만든 v2 자료는 현재 조사 명령에서 지원하지 않는다.

공통 계정 별칭은 `configs/account_aliases.yaml` 및 `verify/accounts.py`의 이름 정규화를 재사용한다. 금액은 기존 Decimal 처리, 동일 공시 필터, 중복 충돌 보류 정책을 유지한다.

## 일부만 확인된 결과

숫자는 확보했으나 설명이 부족하면 `findings.status`는 `partial`이다. `component_status`의 `metrics`, `ratios`, `explanation`이 각각 `complete`, `unavailable`, `not_requested`로 표시된다. 예를 들어 매출액을 출처와 함께 제시한 뒤 설명은 근거 부족으로 확정하지 못했다고 표시한다. 전체 성공(`draft`)과 구분하고 원래 종료 사유도 유지한다.

아직 필요한 작업은 실제 기업 질문으로 검색·모델 성능 검수, 재무제표 주석 밖 사업 현황·경영진 설명 자료의 수집 범위 검토, 상위 Researcher 노드의 다중 도구 루프 연결이다. 원문 대조 판정 전체는 이번 작업의 완료 범위가 아니다.

## 이번 변경의 실행 확인

2026-11-23 기존 고영 XML v1 색인과 실제 GPT·PostgreSQL로 혼합 질문을 실행했다. 첫 검색 5개 이후 주석 하이브리드 재검색에서 신규 근거 5개를 확보했다. 두 번의 검색으로 원인 설명을 확정하지 못해 `max_rounds`로 종료했으며, 최종 결과는 매출액 232,647,218,656원을 보존한 `partial`이었다. 결과는 `data/runs/multi_tool_goyoung_reason_refined.json`에 저장했다. 한 질문의 동작 확인이며 전체 정확도 평가는 아니다. v2 경로 선택은 단위 테스트로 확인했으며 이번 실제 GPT 실행은 v1을 사용했다.

회귀 테스트 361개 중 354개 통과·7개 건너뜀, 실패 0개. 통합 모의 평가 13/13 통과. 실제 기업 질문과 정답을 갖춘 확대 평가가 별도로 필요하다.
