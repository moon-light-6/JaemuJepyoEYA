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

기존 문서 전용 `run_once`, `run_research_loop`는 유지한다. 독립 실행에서는 `--multi-tool`로 선택한다.
상위 그래프의 기본 `make_loop_researcher()`도 다중 도구 루프를 사용한다.
기존 문서 전용 상위 어댑터는 `make_document_researcher()`로 별도 유지한다.

## 상위 Researcher 연결

```text
clarify: 기업·연도·연결/별도 확정
 → brief: 하위 주제와 질문 생성
 → Researcher: 질문마다 다중 도구 루프 실행
   → SQL 숫자 조회 → 필요한 비율 계산 → 필요한 설명 문서 검색
 → Supervisor: 완료 여부 확인 → 미완료 질문만 다음 라운드로 전달
```

`research/multi_tool_adapter.py`가 입력 기준과 질문별 결과를 연결한다.
기업·단일 사업연도·연결/별도·공시 접수번호를 고정하며, 서로 다른 연도의 요청은 임의로 최신 연도로 바꾸지 않고 보류한다.
기준 미지정 시 연결로 진행한다는 안내를 남긴다. 접수번호는 `--rcept-no`, 기업 설정,
동일 기준의 단일 XML 처리 기록 순서로 확인하며 모호하면 보류한다.

숫자와 계산은 성공했지만 설명 근거가 부족하면 `partial`로 남긴다. 다음 상위 라운드에서는
설명만 다시 질문하고 앞서 확인한 숫자와 DB 출처를 보존한다. 질문 수 한도로 미실행한 질문도
완료로 처리하지 않는다. 예산 초과·조건 오류는 자동 재시도를 중단하며 확인된 부분 결과는 유지한다.
상위 라운드 한도와 질문 하나의 문서 재검색 한도(`--loop-rounds`)는 별도다.

질문별 `numbers`, `sources`, `component_status`, `attempts`를 저장하고 근거 ID에
하위 주제·라운드·질문 번호를 붙인다. SQL은 접수번호·계정, XML은 XPath를 보존한다.
임베딩 저장소는 공유하지만 XML 재검색 이력은 질문별로 분리한다. 같은 질문의 상위 재조사는
기존 검색어·근거·검색 세션을 이어받고, 이미 사용한 검색어와 신규 근거가 없는 반복을 중단한다.
상위 결과의 `search_memory`에는 검색어·청크 ID·호출 수만 남긴다. 원문은 실행 메모리에서 유지한다.
새 실행은 별도의 세션 ID로 분리한다. 숫자만 조회하면 색인을 로드하지 않는다.

프로젝트 루트의 CMD 또는 PowerShell에서 실행한다. PostgreSQL 조회 설정과 GPT 키가 필요하며,
설명을 조사할 때에는 해당 공시의 XML 색인도 필요하다.

```bat
python -X utf8 scripts\demo_research.py "서진시스템의 2025년 연결 기준 수익성과 재무 상태를 조사해줘." --rcept-no 20260323001543 --questions 2 --loop-rounds 2 --out data\runs\seojin_upper_research.json
```

진행 과정은 터미널에 출력된다. XML v2 색인은 `--xml-version 2`, 별도 저장소는
`--persist-directory "저장소 경로"`로 지정한다. `--questions`는 하위 주제당 **한 라운드에 실행할 질문 수**다.
현재 그래프는 Supervisor의 조사 결과 확인까지 실행하며 최종 보고서 작성·검증 노드 연결은 별도 작업이다.
조사 역할별 GPT 설정은 유지한다. 주장 추출기의 Qwen 선택과는 독립적이다.

상위 그래프·다중 도구·재조사 연결 테스트(외부 GPT·DB 호출 없이 합성 응답 사용):

```bat
python -X utf8 -m unittest discover -s tests -t . -p test_research_multi_tool.py
```

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

조회 지표는 `configs/account_aliases.yaml`의 모든 항목(현재 12개)을 사용한다.
YAML의 `statements`는 IS/CIS/BS/CF 중 실제 조회할 표를 제한하며, 잘못된 표에서 같은 이름을 가져오지 않는다.
`financial_catalog.py`가 별칭·표시 이름·구조화 출력 지표 목록과 Brief/Planner 지원 안내를 연결한다.
YAML 변경 후 프로세스를 재시작해야 한다. DB에 값이 없으면 생성하지 않고 누락으로 남긴다.

지원·미지원 요구가 섞이면 `unsupported_items`에 미지원 요구만 기록하고 지원 지표는 실행한다.
완료하지 못한 요구가 있으면 전체 완료 대신 `partial` 또는 `insufficient_evidence`다.
현재 현금및현금성자산(BS), 영업·투자·재무활동현금흐름(CF)도 조회할 수 있다.

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

첫 검색부터 벡터·제목·핵심어·BM25를 결합한다. 같은 원문 문서·제목·XML 파일 안에서 앞뒤 청크를
추가하며, 반환량은 최대 `2 * top_k`다. 이전 청크보다 새 근거를 우선한다. 원인·감사·경영진 질문은
재검색에서도 주석으로 범위를 좁히지 않는다. 섹션을 명시했다면 필터를 완화하지 않는다.
키워드 및 인접 검색은 최대 5천 청크까지이며, 초과 시 벡터 검색만 사용한다.

XML 문서 생성은 기존 재무제표·주석 뒤에 선택 기준 감사보고서의 감사의견과 사업보고서의
경영진 분석·회계감사 의견 절을 추가한다. 기존 문서 ID는 유지한다. 공통 사업보고서 절은
`scope_basis=report_context`로 구분하므로 실제 문장에서 연결/별도를 확인해야 한다.
`*_xml_pipeline.json`의 `research_coverage`에 실제 추출된 감사·경영진 분석 문서 수가 기록된다.
원본 ZIP에 없는 내용은 추가하지 않으며 외부 웹 검색은 수행하지 않는다.

다른 기업의 기존 색인에도 추가 범위를 반영하려면 해당 기업 XML 파이프라인을 다시 실행한다.
검수한 평가셋은 유지되며 다음 명령은 해당 기업의 로컬 XML 컬렉션을 다시 만든다.

```bat
python -X utf8 scripts\pipelines\run_pipeline_xml.py --company "서진시스템" --corp-code 00838005 --fiscal-year 2025 --rcept-no 20260323001543 --statement-scope consolidated --rebuild-index
```

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
