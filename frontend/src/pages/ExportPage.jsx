import { useState } from "react";
import AppShell from "../components/AppShell.jsx";

const MODES = ["원본 그대로", "승인 수정 반영", "항목 제외", "수정 + 제외"];

export default function ExportPage() {
  const [mode, setMode] = useState(3);
  const [fmt, setFmt] = useState("PDF");
  return (
    <AppShell variant="work" title="출력 센터" pill="출력 계획">
      <div className="page-head">
        <h2 className="hero-title left">어떤 보고서를 만들까요?</h2>
        <p className="muted">승인한 정정만 반영하고, 제외할 내용은 파일 생성 전에 미리 확인할 수 있어요.</p>
      </div>
      <div className="two">
        <div className="card">
          <h4>출력 요청</h4>
          <p className="muted small">자연어로 입력해도 괜찮아요</p>
          <textarea className="textarea" placeholder="예) 틀린 건 수정하고 주석은 빼줘" />
          <h4 className="mt">출력 모드</h4>
          <div className="grid2">
            {MODES.map((m, i) => (
              <button key={m} className={`opt ${mode === i ? "on" : ""}`} onClick={() => setMode(i)}>
                {mode === i ? <i className="ri-check-line" /> : <i className="ri-checkbox-blank-circle-line" />} {m}
              </button>
            ))}
          </div>
          <h4 className="mt">파일 형식</h4>
          <div className="row between">
            <select className="form-input w-sm" value={fmt} onChange={(e) => setFmt(e.target.value)}>
              <option>PDF</option><option>DOCX</option>
            </select>
            <button className="btn btn-primary">계획 미리보기</button>
          </div>
        </div>
        <div className="card">
          <div className="row between"><h4>출력 계획 미리보기</h4><span className="tag orange">확인 필요</span></div>
          <p className="muted small mt">수정 적용</p>
          <div className="plan green"><i className="ri-check-line ico-sq" /><div><strong>승인한 정정 1건 반영</strong><small>claim_001 · 매출액 100조 원 → 120조 원</small></div></div>
          <p className="muted small mt">제외 대상</p>
          <div className="plan yellow"><i className="ri-error-warning-line ico-sq warn" /><div><strong>주석 섹션</strong><small>section_08 · 다른 표·주장에 미치는 영향 없음</small></div></div>
          <p className="muted small mt">일관성 검사</p>
          <div className="row gap"><span className="chip-soft">검사 대기</span><span className="muted small">계획을 확정하면 {fmt} 생성 후 최종 검사합니다.</span></div>
          <div className="row end mt"><button className="btn btn-primary">계획 확정 · {fmt} 생성</button></div>
        </div>
      </div>
      <p className="center muted small mt-lg">작업은 임시 보관되며 만료 후 자동 삭제됩니다.</p>
    </AppShell>
  );
}
