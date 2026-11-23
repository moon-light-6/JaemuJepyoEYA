# loop_qa: 6가지 유형의 조사 평가

## 평가 범위

기본 평가셋은 **가상 기업과 가상 숫자**로 구성된 12개 시나리오다. 공시에서 검수한
실제 기업 정답으로 표시하지 않는다. API 키, OpenDART 호출, 실제 DB, 임베딩 모델 없이 실행한다.

기본 모의 실행에서 실제로 동작하는 코드는 다음과 같다.

- SQL 조회: `FinancialSQLTool`의 스키마 프롬프트·구조화 출력·SQL 검사. 모델 응답과 DB 결과만 가짜다.
- 계산: 기존 `ratios.operating_profit_margin`. SQL 입력이 누락되거나 연도가 다르면 계산하지 않는다.
- 문서 조사: 기존 `run_research_loop`의 LangGraph, 근거 누적, 출처 검사, 재검색, 종료 제어.
  검색 결과와 LLM의 충분성 평가·답변 내용만 시나리오로 고정한다.
- 숫자+설명: 평가용 어댑터가 위 구성요소들을 연결한다.

**모의 실행 통과는 실제 LLM의 이해·도구 선택·검색 정확도가 검증됐다는 뜻이 아니다.**
기본 `mock`은 최초 구성요소 기준을 유지한다. 새 `integration-mock` 모드는 추가된 생산용
`planner.py`, `router.py`, `executor.py`, `calc_tool.py`, `tool_graph.py`를 실제로 연결해 평가한다.
이 모드에서도 LLM 계획 응답과 외부 입력은 고정하므로 실제 모델의 계획 정확도를 검증한 것은 아니다.

## 기본 질문

| ID | 유형 | 확인하는 동작 |
|---|---|---|
| LQ01 | 숫자만 조회 | SQL 금액·단위·기준·출처 보존 |
| LQ02 | 설명 문단 검색 | 설명 문구와 실제 확보한 근거 인용 |
| LQ03 | SQL 후 계산 | 매출·영업이익으로 20% 계산 및 입력 출처 유지 |
| LQ04 | 숫자+설명 | SQL과 문서 검색을 모두 사용 |
| LQ05 | 답변 보류 | 빈 조회 결과를 금액 0으로 바꾸지 않음 |
| LQ06 | 재검색 | 첫 근거 부족 → 두 번째 검색에서 근거 확보 |
| LQ07 | 반복 중단 | 동일 청크만 재검색되면 `no_new_evidence` |
| LQ08 | 반복 중단 | 같은 다음 검색어를 재사용하지 않고 `repeated_query` |
| LQ09 | 반복 중단 | 최대 검색 횟수에 도달하면 `max_rounds` |
| LQ10 | 계산 불가 | 매출 0으로 나누지 않고 `invalid_denominator` |
| LQ11 | 기준 불일치 | 서로 다른 연도 값으로 계산하지 않음 |
| LQ12 | 도구 오류 | DB 오류를 숫자나 정상 답변으로 위장하지 않음 |

## 실행 방법

프로젝트 루트에서 활성화된 가상환경으로 아래 한 줄을 실행한다(CMD).

```bat
python -X utf8 evaluation\run_eval.py --mode mock --out data\runs\loop_qa_report.json --observations-out data\runs\loop_qa_observations.jsonl
```

- 터미널: 질문별 진행 상황, 6개 유형의 통과 수, 실패 유형.
- 보고서 JSON: 전체·유형별 집계와 질문별 검사 상세.
- 관측값 JSONL: 도구 이력, 확보한 근거, 반환 숫자, 답변, 종료 사유.
- 종료 코드: 0=모든 질문 통과, 1=평가 실패, 2=입력 자료 오류.

새 다중 도구 그래프를 같은 12개 시나리오로 평가하려면 다음 명령을 사용한다.

```bat
python -X utf8 evaluation\run_eval.py --mode integration-mock --out data\runs\loop_qa_integration_report.json --observations-out data\runs\loop_qa_integration_observations.jsonl
```

실제 GPT와 PostgreSQL 실행은 `docs/multi_tool_research.md`를 참고한다.

## 자료형과 함수

| 파일 | 주요 클래스·함수 | 역할 |
|---|---|---|
| `evaluation/loop_qa.py` | `LoopQACase`, `ExpectedResult` | 질문·기준·정답 계약 |
| 동일 | `FixtureSpec` | 정답과 분리된 모의 입력 |
| 동일 | `LoopObservation`, `NumericAnswer`, `QAEvidence`, `ToolEvent` | 평가할 시스템 출력 |
| 동일 | `load_jsonl` | 형식·중복 ID 검증 |
| `evaluation/loop_fixture_runner.py` | `FixtureModel`, `FixtureSQLStore`, `FixtureSearch` | 네트워크 없는 테스트 대역 |
| 동일 | `run_fixture_case` | 구성요소 실행과 관측값 생성 |
| `evaluation/metrics/loop_metrics.py` | `evaluate_case`, `summarize` | 항목별 채점과 집계 |
| `evaluation/run_eval.py` | `evaluate_observations`, `run_mock`, `main` | 실행·채점·보고서 저장 |

모든 추가 클래스와 함수에 역할을 설명하는 한글 docstring을 달았다.

## 채점 정책

- 금액은 `Decimal`로 비교한다. 정답의 `tolerance`는 **지정 단위의 절대 오차**이며 기본값은 0이다.
- 단위가 다르면 숫자가 같아도 실패한다. 자동 단위 환산은 하지 않는다.
- 회사·실적 연도(`context.year`)·연결/별도·통화가 맞아야 한다.
- 각 숫자는 `source_ids`, 답변은 `cited_source_ids`로 실제 확보한 `evidences`에 연결되어야 한다.
- `expected.tools`는 해당 사례에 기대하는 정확한 도구 실행 순서다. 실제 시스템에서 여러 유효 경로를
  허용하려면 평가 계약을 확장해야 하며 현재 모든 질문에 유일한 경로가 있다고 주장하지 않는다.
- 설명은 지정 문구 포함 여부만 자동 평가한다. 의미·인과관계의 완전한 검증은 사람 검수나 별도 평가가 필요하다.
- 보류·오류 답변에는 확정 숫자를 넣지 않고 이유를 남겨야 한다.
- 재검색은 실제 `stop_reason`과 검색 횟수를 비교한다. 첫 검색 실패 후 성공적으로 복구한 경우는 허용한다.
- 실행 결과가 빠진 질문도 분모에서 빼지 않고 실패로 기록한다. 중복 ID나 알 수 없는 ID는 거부한다.
- 일부 유형만 평가했다면 나머지는 `pass_rate: null`로 표시한다.

## 실제 시스템 결과 평가로 확장

생산 루프가 완성되면 출력물을 `LoopObservation` JSONL로 변환해 다음처럼 채점한다.
기존 `run_loop.py`나 `run_sql.py` 결과 파일과는 형식이 다르므로 그대로 넣지 않는다.

```bat
python -X utf8 evaluation\run_eval.py --mode predictions --dataset evaluation\dataset\loop_qa.jsonl --predictions data\runs\loop_qa_observations.jsonl --out data\runs\loop_qa_recheck.json
```

위 예시는 모의 관측값을 다시 채점하는 형식 확인용이다. 실제 성능을 평가하려면 별도 평가셋에
사람이 검수한 실제 질문·정답·단위·출처를 넣고 `data_origin: verified_source`와 검수 근거를 기록한다.
모의 fixture는 정답 예측기로 사용하지 않고, 실제 생산 루프의 관측값을 제출해야 한다.
제출 관측값만으로 도구가 정말 호출됐는지 또는 출처 내용이 진짜인지까지 자동 인증하지는 않는다.

## 단위 테스트

```bat
python -X utf8 -m unittest discover -s tests -p test_loop_qa.py -v
```

정상 사례 외에 금액·단위·연도·범위·출처·도구 순서·검색 횟수를 일부러 바꿔 평가가 실패하는지 확인한다.
