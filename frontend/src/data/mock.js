// Figma "최프_재무제표야~" 문구 기준 목 데이터. 실제 데이터가 아님.
export const RECENT_JOBS = [
  { id: "job-1", title: "2025 재무 분석 보고서", sub: "검증 완료" },
  { id: "job-2", title: "삼성전자 3Q 분석", sub: "초안" },
  { id: "job-3", title: "기업가치 리서치", sub: "검증 중" },
];

export const QUICK_STARTS = [
  { title: "전체 검증", desc: "문장·표의 수치와 기준을 전체 확인", to: "/progress" },
  { title: "틀린 주장만 확인", desc: "오류 판정된 항목을 먼저 보기", to: "/chat/result" },
  { title: "수정본 만들기", desc: "승인한 수정만 반영해 출력", to: "/chat/generate" },
];

export const PROMPT_SUGGESTIONS = [
  { title: "SK하이닉스 2026년 상반기", desc: "매출과 영업이익을 중심으로 분석해줘", icon: "ri-arrow-right-up-line" },
  { title: "삼성전자 2025년 실적", desc: "연간 실적과 전년 대비 변화를 정리해줘", icon: "ri-file-list-line" },
  { title: "현대자동차 최근 3개년", desc: "매출액과 영업이익 추이를 비교해줘", icon: "ri-bar-chart-box-line" },
  { title: "LG에너지솔루션 재무 분석", desc: "부채비율과 재무 안정성을 살펴봐줘", icon: "ri-shape-line" },
];

export const STEPS = [
  { title: "문서 구조 파싱", desc: "문단 · 표 · 셀 · 페이지 위치 인식" },
  { title: "주장 추출", desc: "검증할 수치·기간·기준 후보 생성" },
  { title: "공시 근거 비교", desc: "금액 · 기준 · 기간을 대조하고 있어요" },
  { title: "결과 집계", desc: "핵심 주장 최대 5개를 선정합니다" },
];

// status: error(오류) | hold(보류) | ok(정상)
export const CLAIMS = [
  {
    id: "c1", status: "error", label: "오류", claim: "매출액은 100조 원이다.",
    loc: "p.2 · 본문 블록 12", page: 2, reported: "100조 원", disclosed: "120조 원", match: "불일치",
    fix: "매출액은 120조 원이다.", source: "OpenDART · 사업보고서 · 별도 · 2025년",
  },
  { id: "c2", status: "hold", label: "보류", claim: "전년 대비 5% 증가했다.", sub: "기준·기간 확인 필요", page: 2 },
  { id: "c3", status: "ok", label: "정상", claim: "부채비율은 전년보다 하락했다.", sub: "근거 2개와 일치", page: 3 },
  { id: "c4", status: "error", label: "오류", claim: "연결 기준 영업이익은 3조 원이다.", sub: "연결/별도 기준 불일치", page: 4 },
  { id: "c5", status: "ok", label: "정상", claim: "총자산은 증가했다.", sub: "공시 수치와 일치", page: 5 },
];
