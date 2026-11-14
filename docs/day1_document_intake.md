# 공식 문서 확보·점검

이 단계는 공식 원문을 확보하고 출처를 남기는 범위까지만 담당한다. DuckDB, 청킹,
임베딩, 검색 인덱스, RAG는 이 단계에서 만들지 않는다.

## 입력과 저장 경로

- 기업·사업연도·접수번호·DART PDF 문서번호: `configs/companies.yaml`
- XML 원본: `data/raw/dart/{corp_code}/{fiscal_year}/{rcept_no}.zip`
- XML 구조·표·주석 결과: `data/processed/dart/{corp_code}/{fiscal_year}/`
- PDF 원본: `data/raw/dart/{corp_code}/{fiscal_year}/{rcept_no}.pdf`
- PDF 출처 메타데이터: `data/raw/dart/{corp_code}/{fiscal_year}/{rcept_no}_pdf.json`
- PDF 페이지별 텍스트: `data/processed/dart/{corp_code}/{fiscal_year}/{rcept_no}_pdf_pages.json`
- XML 최종 점검: `data/processed/dart/{corp_code}/{fiscal_year}/{rcept_no}_document_scope.json`

XML의 출처 위치는 `xpath`, PDF의 출처 위치는 1부터 시작하는 `page_number`다.
두 형식의 내용이 같다고 가정하지 않으며, 각 결과 JSON의 `format_validation`에서
상호 비교 수행 여부를 확인한다.

## 실행

먼저 기존 XML 결과를 최종 점검한다.

```powershell
.\.venv\Scripts\python.exe scripts\check_documents.py `
  --zip-path data\raw\dart\00126380\2025\20260310002820.zip `
  --structure-path data\processed\dart\00126380\2025\20260310002820_structure.json `
  --tables-path data\processed\dart\00126380\2025\20260310002820_consolidated_tables.json `
  --notes-path data\processed\dart\00126380\2025\20260310002820_consolidated_notes.json `
  --output-path data\processed\dart\00126380\2025\20260310002820_document_scope.json
```

그다음 설정된 DART의 공식 PDF를 수집하고 PyMuPDF로 페이지별 텍스트를 점검한다.

```powershell
.\.venv\Scripts\python.exe scripts\collect_pdf.py `
  --corp-code 00126380 --fiscal-year 2025
```

PDF 수집은 DART 공식 HTTPS 도메인, PDF 파일 서명, PyMuPDF 개방 여부를 확인한다.
이미 있는 PDF는 URL과 SHA-256이 메타데이터와 모두 일치할 때만 재사용한다. 일치하지
않는 기존 파일은 덮어쓰지 않는다.

## 삼성전자 2025 최종 확인 기록

- 접수번호: `20260310002820`, DART 문서번호: `11104488`
- DART 다운로드 화면의 문서명: `[삼성전자]사업보고서(2026.03.10).pdf`
- 공식 원본 URL: `https://dart.fss.or.kr/pdf/download/pdf.do?rcp_no=20260310002820&dcm_no=11104488`
- 로컬 PDF SHA-256과 출처 메타데이터·페이지 점검 JSON의 SHA-256은 모두
  `8ff8a4a975bd02776395d0f890839633bfede8bd4d7699b7a417183780cdfa0a`로 일치한다.
- XML은 사업보고서(`20260310002820.xml`), 별도감사보고서(`_00760.xml`),
  연결감사보고서(`_00761.xml`)로 구분된다. 연결재무제표 5종과 2025·2024 비교 열은
  `_00761.xml`에서 확인했고, PDF도 물리 페이지 83~90에서 연결재무제표임을 확인했다.
- PDF의 연결재무제표 주석은 91쪽에서 시작해 195쪽의 주석 34까지 확인했으며,
  별도재무제표는 197쪽에서 시작한다. 사업 개요는 33쪽, 감사의견은 338쪽이다.
  위 숫자는 PDF 파일의 물리 페이지 번호이며, 화면 하단에 인쇄된 페이지 번호와 다를 수 있다.

### PDF 텍스트 품질과 수동 대조

자동 점검 결과는 573쪽 모두 비공백이고 U+FFFD 대체문자·비정상 제어문자가 없었다.
다만 일반 텍스트 추출은 표의 시각적 행·열 배치를 보장하지 않으므로 아래 페이지는 후속 사용
전에 PDF 화면과 수동 대조한다.

- 연결재무제표 표: 83~90쪽
- 연결 주석 시작·종료 및 표: 91쪽, 195쪽
- 감사의견 표: 338쪽
- 196쪽: 텍스트가 페이지 푸터뿐이어서 의도된 빈 전환 페이지인지 수동 확인 필요

사업 개요 33쪽과 주석·감사의견의 위 표본 페이지는 화면 렌더링으로 제목과 표 배치를 확인했다.
하지만 PDF와 XML의 문장·수치·표 구조가 전체적으로 동일한지는 검증하지 않았으며, OCR도
수행하지 않았다.

## 최종 회귀 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 다음 담당자 참고

후속 단계는 `*_pdf_pages.json`의 `pages[].text`와 `pages[].source.page_number`, XML
JSON의 XPath 출처를 읽어 사용할 수 있다. PDF 텍스트는 OCR·정규화·문자 보정을 하지
않은 추출값이며, `blank_page_numbers`, `replacement_character_page_numbers`,
`character_encoding_review_required`를 먼저 확인해야 한다.
