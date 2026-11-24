# XML 평가 정답표 및 공용 ChromaDB 적재 가이드

> **대상:** 팀원별 담당 기업 · **실행 위치:** 각 팀원 PC의 프로젝트 루트(Windows CMD)  
> **저장 위치:** 평가 정답표(v1·v2)는 **Supabase**, 검색용 XML v2 청크·임베딩은 **현규 PC의 공용 ChromaDB**

## 1. 적재 대상과 사전 확인

| 데이터 | 저장소 | 실행 파일 |
|---|---|---|
| 검증된 XML 평가 정답표(v1·v2) | Supabase `fin.xml_eval_ground_truth_v2` | `scripts/storage/load_xml_eval_ground_truth.py` |
| 검색용 XML v2 청크 및 임베딩 | **현규 PC** 공용 ChromaDB | `scripts/storage/load_shared_chroma.py` |

두 적재 작업은 **서로 별개**입니다. Supabase에 정답표를 넣어도 ChromaDB에는 자동 반영되지 않습니다. OpenDART 원천 재무 수치(`fin.dart_financial_accounts`) 적재는 별도 `docs/supabase_data_loading.md`를 참고합니다.

먼저 사용 중인 브랜치에 스크립트가 있는지 확인합니다.

```cmd
git branch --show-current
dir scripts\storage\load_xml_eval_ground_truth.py
dir scripts\storage\load_shared_chroma.py
```

**확인 당시 GitHub 기준(2026-11-24):** 두 스크립트는 `data/pdf_processing` 브랜치의 `scripts/storage/` 폴더에 있습니다. 이전 작업 브랜치에는 없거나 `scripts/` 바로 아래에 있을 수 있으므로, **현재 체크아웃한 브랜치에 두 파일이 같은 경로로 있다고 가정하면 안 됩니다.** 필요한 스크립트가 없으면 최신 `data/pdf_processing`를 받거나 담당자에게 작업 브랜치로 반영해 달라고 요청한 후 진행하세요.

## 2. 담당 공시 정보 설정

아래는 **CJ ENM 예시**입니다. 본인의 기업 정보로 바꿔 입력하세요.

```cmd
set "CORP_CODE=00265324"
set "YEAR=2025"
set "SCOPE=consolidated"
set "RCEPT_NO=20260318001469"
```

- `CORP_CODE`: DART **기업고유번호 8자리**(종목코드 아님)
- `SCOPE`: 연결 `consolidated` / 별도 `separate`
- `RCEPT_NO`: 공시 **접수번호 14자리**

---

## 3. XML 평가 정답표 → Supabase

### ① 정답 파일 확인

```cmd
dir evaluation\dataset\xml\%CORP_CODE%\%YEAR%\%SCOPE%\%RCEPT_NO%_retrieval.json
dir evaluation\dataset\xml\%CORP_CODE%\%YEAR%\%SCOPE%\%RCEPT_NO%_retrieval_v2.json
```

- **v1 필수**, v2는 보충 문항이 있을 때만 사용합니다.
- `annotation_status=verified`인 문항만 적재합니다. `excluded`·`rejected`는 제외하며 그 밖의 미검증 상태가 남아 있으면 실제 적재가 중단됩니다.
- Supabase의 `fin.xml_eval_ground_truth_v2` 테이블이 준비되어 있어야 합니다. 프로젝트 루트 `.env`에 팀에서 제공한 **`DATABASE_URL`**도 필요합니다.

### ② DRY-RUN (DB 변경 없음)

```cmd
python scripts\storage\load_xml_eval_ground_truth.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --rcept-no %RCEPT_NO%
```

출력된 **기업 정보, v1·v2 파일명, `verified` 건수, 적재 대상 합계**를 확인합니다. 이 단계는 JSON을 검증하며 Supabase에는 연결하지 않습니다.

### ③ 실제 적재

```cmd
python scripts\storage\load_xml_eval_ground_truth.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --rcept-no %RCEPT_NO% --apply
```

`COMMIT 완료: 동일 공시 N건 → M건` 출력에서 **M이 DRY-RUN의 적재 대상 건수와 일치**하는지 확인하세요.

> **중요:** `--apply`는 같은 **기업·연도·연결/별도·접수번호**의 기존 평가 정답표를 삭제하고, 현재 검증된 v1·v2 전체로 **교체**합니다. 다른 공시의 데이터는 삭제 대상이 아닙니다. 반드시 최종 검수된 파일로 실행하세요.

### ④ Supabase SQL Editor에서 결과 확인

```sql
SELECT dataset_version, COUNT(*) AS question_count
FROM fin.xml_eval_ground_truth_v2
WHERE corp_code = '00265324'
  AND fiscal_year = 2025
  AND statement_scope = 'consolidated'
  AND rcept_no = '20260318001469'
GROUP BY dataset_version
ORDER BY dataset_version;
```

**SQL의 기업 정보 4개를 본인 값으로 변경**하세요. v1·v2별 건수가 DRY-RUN 결과와 일치해야 합니다. v2가 없는 기업은 v1만 표시됩니다.

---

## 4. XML v2 청크·임베딩 → 현규 PC 공용 ChromaDB

**서버 관리:** 현규 PC의 Docker/ChromaDB 서버는 현규가 운영합니다. **다른 팀원은 Docker 서버를 실행할 필요 없이**, 본인 PC에서 Python 스크립트로 원격 적재합니다.

### ① Tailscale 및 서버 주소 설정

- 팀에서 승인받은 **같은 Tailscale tailnet**에 접속합니다.
- **현규에게 새 Tailscale Serve 호스트명**(예: `PC이름.테일넷.ts.net`)을 받아 프로젝트 루트 `.env`에 입력합니다.
- **옛 학원 PC 주소 `desktop-hujuvss.tailb384f3.ts.net`은 사용하지 않습니다.** 새 호스트명은 이 문서에서 확인할 수 없으므로 현규에게 확인해야 합니다.

```dotenv
CHROMA_SHARED_HOST=현규_PC의_실제_호스트명
```

호스트명에 `https://`나 `/`를 붙이지 않습니다. 아래 명령은 **Tailscale Serve가 HTTPS 443으로 ChromaDB에 연결되도록 설정된 경우**의 현재 적재 스크립트 기준입니다.

```cmd
"C:\Program Files\Tailscale\tailscale.exe" status
python -c "import os,chromadb; from dotenv import load_dotenv; load_dotenv(); c=chromadb.HttpClient(host=os.environ['CHROMA_SHARED_HOST'],port=443,ssl=True); print('Heartbeat:',c.heartbeat())"
```

`Heartbeat:` 뒤 응답이 나오면 **서버 연결**을 확인한 것입니다. 접속에 실패하면 현규에게 Docker 컨테이너·Tailscale Serve의 실행 상태를 확인받으세요. 서버는 현규 PC에서 수동으로 실행하므로, PC가 꺼져 있거나 절전 상태이면 접속되지 않습니다.

### ② XML v2 파일 및 로컬 임베딩 확인

```cmd
dir data\processed\xml\dart\%CORP_CODE%\%YEAR%\%SCOPE%\%RCEPT_NO%_xml_chunks_v2.json
python scripts\storage\load_shared_chroma.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --rcept-no %RCEPT_NO%
```

- `dir`에서 파일을 찾지 못하면 **적재를 진행하지 마세요.** 스크립트가 `FileNotFoundError`로 종료됩니다. 해당 기업의 XML v2 청킹이 끝났는지, 다른 PC에 생성된 파일이 있는지 먼저 확인합니다.
- DRY-RUN(`--apply` 없음)도 **공용 서버에 접속해 읽기만** 합니다. 서버에는 쓰지 않지만, 위 ①의 접속 설정이 되어 있어야 실행됩니다.

DRY-RUN 출력의 `XML v2`, `공용 저장`, `추가 필요`, `로컬 임베딩` 수를 확인합니다.

- `추가 필요: 0개` → 해당 입력 파일 기준, 새로 추가할 청크 없음
- `로컬 임베딩`이 XML v2 청크 수와 일치 → 기존 벡터 재사용 가능
- `로컬 임베딩: 0개` → `--apply` 시 KURE-v1 임베딩을 새로 생성할 수 있음. 기존 임베딩이 다른 PC에 있다면 **그 PC에서 적재**하는 것이 재생성을 줄입니다. 임베딩 모델 다운로드와 연산에 시간이 걸리고, GPU 메모리를 많이 쓰는 다른 작업(예: LLM 서버)이 실행 중이면 메모리 부족이 날 수 있으므로 먼저 종료하세요.

### ③ 실제 적재 및 검증

```cmd
python scripts\storage\load_shared_chroma.py --corp-code %CORP_CODE% --year %YEAR% --scope %SCOPE% --rcept-no %RCEPT_NO% --apply
```

정상 완료 시 `공용 적재 검증 완료: N개`, `Match: True`를 확인합니다. `Match: True`는 **청크 ID와 본문 일치**를 뜻하며, 임베딩 값의 동일성이나 검색 성능까지 검증한 것은 아닙니다.

### ④ 공용 ChromaDB 전체 현황 조회

```cmd
python -c "import os,chromadb; from dotenv import load_dotenv; load_dotenv(); c=chromadb.HttpClient(host=os.environ['CHROMA_SHARED_HOST'],port=443,ssl=True); rows=sorted((x.name,x.count()) for x in c.list_collections()); [print(n,'|',v) for n,v in rows]; print('컬렉션:',len(rows),'전체 청크:',sum(v for n,v in rows))"
```

이 명령은 **현규 PC의 공용 서버를 읽기만** 합니다. 서버 이전 전 학원 PC에서 기록한 **7개 컬렉션·1,402개 청크(2026-11-24 초기 기록)**는 비교 참고값이며, 현재 최신 건수로 단정하지 않습니다.

---

## 5. 오류·주의사항

| 상황 | 확인할 항목 |
|---|---|
| Supabase `v1 정답 파일이 없습니다` | 기업고유번호·접수번호·JSON 파일 경로 |
| Supabase 미검증 문항 오류 | `annotation_status` 및 검수 완료 여부 |
| Supabase 연결/테이블 오류 | `.env`의 `DATABASE_URL`, DB 권한, 테이블 존재 여부 |
| ChromaDB 스크립트 파일 없음 | **현재 브랜치**에 `scripts/storage/load_shared_chroma.py` 반영 여부 |
| ChromaDB `FileNotFoundError` (`..._xml_chunks_v2.json`) | `data\processed\xml\dart\...` 경로의 XML v2 청크 파일 존재 여부, 청킹 작업 완료 여부 |
| ChromaDB DNS·접속 실패 | 현규 PC의 실제 호스트명, Tailscale 승인·연결, Serve 및 Docker 상태 |
| ChromaDB 원본 해시·본문 불일치 | XML v2 파일 버전과 기존 공용 컬렉션의 일치 여부. **임의 삭제 금지** |

- `.env`, DB 비밀번호, ChromaDB 영속 저장 폴더는 **Git에 올리지 않습니다**.
- 공용 ChromaDB 컬렉션을 임의로 삭제하거나 덮어쓰지 않습니다.
- **Supabase 정답표 적재와 ChromaDB 검색 성능 검증은 별개**입니다.
