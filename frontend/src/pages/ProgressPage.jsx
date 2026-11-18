import { useNavigate } from "react-router-dom";
import AppShell from "../components/AppShell.jsx";
import { STEPS } from "../data/mock.js";

// TODO(API): 진행률/단계는 작업 상태 API 로 교체. 지금은 고정 값.
const CURRENT = 2; // 0-based

export default function ProgressPage() {
  const navigate = useNavigate();
  return (
    <AppShell variant="work" title="검증 진행 중" pill="검증 중">
      <div className="page-head">
        <h2 className="hero-title left">문서를 읽고, 주장과 공시 근거를<br />차례대로 비교하고 있어요.</h2>
        <p className="muted">작업을 닫아도 잠시 뒤 결과 화면에서 다시 확인할 수 있어요.</p>
      </div>
      <div className="split">
        <div className="card">
          <div className="row between"><strong>검증 진행률</strong><strong className="accent">50%</strong></div>
          <div className="bar"><i style={{ width: "50%" }} /></div>
          <p className="muted small">15 / 30개 주장 처리 완료</p>
          <ul className="steps">
            {STEPS.map((s, i) => (
              <li key={s.title} className={i < CURRENT ? "done" : i === CURRENT ? "now" : ""}>
                <span className="dot">{i < CURRENT ? <i className="ri-check-line" /> : i + 1}</span>
                <strong>{s.title}</strong><small>{s.desc}</small>
              </li>
            ))}
          </ul>
          <div className="row end mt">
            <button className="btn btn-primary" onClick={() => navigate("/review")}>결과 보기</button>
          </div>
        </div>
        <div className="card side-info">
          <h4>안내</h4>
          <p className="muted small">금액이 맞아도 기준이나 기간이 불분명하면 '보류'로 남겨요.</p>
          <span className="tag orange">자동 수정 없음</span>
          <p className="muted small mt">결과에서 근거를 보고 직접 승인할 수 있어요.</p>
        </div>
      </div>
    </AppShell>
  );
}
