# 조사 진행 과정 보기

`scripts/run_loop.py`와 `scripts/run_sql.py`는 기본적으로 진행 상황을 터미널에 즉시 출력한다.
각 줄에 현재 시각과 실행 시작 후 경과 시간을 표시한다. 진행 로그는 stderr로,
최종 JSON은 stdout과 `--out` 파일로 분리한다. JSON 결과 구조를 진행 로그로 대체하지 않는다.

## 재검색 과정 확인

프로젝트 루트의 활성화된 가상환경에서 CMD 한 줄로 실행한다.
아래 명령은 기존 삼성전자 Chroma 색인으로 문서 검색·평가·재검색을 수행한다.

```bat
python -X utf8 scripts\run_loop.py --question "삼성전자 2025년 연결 기준 매출 변화 원인을 근거와 함께 설명해줘." --max-rounds 3 --out data\runs\research_progress.json
```

표시되는 단계는 다음과 같다.

1. 현재 검색 횟수와 실제 검색어
2. 새로 확보한 근거의 출처 ID·파일명·PDF 페이지·청크 ID
3. 반환 근거 수·신규 근거 수·누적 근거 수
4. 모델의 근거 충분성 평가 요약과 부족한 정보
5. 재검색을 선택한 경우 다음 검색어
6. 답변 생성, 출처 검사 실패 시 수정 요청
7. 종료 이유·검색 횟수·LLM 호출 횟수·결과 저장 위치

재검색은 평가 결과에 따라 결정된다. 한 번에 충분하면 검색 1회로 종료하므로
항상 최대 횟수만큼 반복하는 것은 아니다. 첫 검색은 임베딩 모델 로딩 때문에 오래 걸릴 수 있다.

주요 종료 코드:

| 코드 | 의미 |
|---|---|
| sufficient_evidence | 평가에서 근거가 충분하다고 판단 |
| max_rounds | 최대 검색 횟수 도달 |
| no_new_evidence | 검색 결과에 새로운 청크가 없음 |
| repeated_query | 이미 사용한 검색어가 반복됨 |
| no_next_query | 부족 판정이지만 다음 검색어가 없음 |
| tool_error | 검색 도구 오류 |
| answer_insufficient | 답변 생성 단계에서도 근거 부족으로 판단 |
| generation_validation_failed | 답변 출처·형식 검사 재실패 |

위 기본 명령은 문서 검색용이다. SQL·계산·설명을 선택하는 새 모드는 `--multi-tool`이며
사용법은 `docs/multi_tool_research.md`를 참고한다.
근거 충분성 평가와 공식 수치 일치 검증도 구분해야 한다.

## SQL 조회 과정 확인

```bat
python -X utf8 scripts\run_sql.py --question "에코프로비엠의 2025년 연결 매출액을 공시별로 구분해서 원 단위와 출처 페이지, 청크 ID로 조회해줘." --out data\runs\sql_test.json
```

스키마 기반 SQL 생성 → 구문·기간 검사 → 필요 시 SQL 수정 → PostgreSQL 읽기 전용 조회 → 결과 저장을 표시한다.
접속 문자열과 API 키는 진행 로그에 넣지 않는다. `--plan-only`이면 DB를 조회하지 않으며 로그에도 미실행임을 표시한다.

진행 로그를 숨기려면 두 명령 중 어느 것이든 `--quiet`를 추가한다.
라이브러리 내부에서 출력하는 임베딩 로딩 메시지까지 숨기는 옵션은 아니다.
진행 로그를 별도 파일에 남기려면 CMD에서 명령 마지막에 `2> data\runs\progress.log`를 붙인다.

Python 함수에서 사용하려면 `progress=callback`으로 문자열 수신 함수를 전달한다.
콜백을 생략하면 라이브러리는 자체 진행 로그를 출력하지 않는다.
