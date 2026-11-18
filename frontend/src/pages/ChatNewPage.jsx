import { useNavigate } from "react-router-dom";
import AppShell from "../components/AppShell.jsx";
import { QUICK_STARTS } from "../data/mock.js";

export default function ChatNewPage() {
  const navigate = useNavigate();
  return (
    <AppShell variant="chat" title="새 검증" pill="보안 보관" pillClass="pill-green">
      <div className="center-col">
        <h2 className="hero-title">무엇을 검증할까요?</h2>
        <p className="hero-sub">보고서를 올리거나, 검증하고 싶은 내용을 먼저 입력해 보세요.</p>

        <div className="card upload-mini">
          <p className="muted">재무 보고서를 업로드하거나 메시지를 입력하세요.</p>
          <div className="file-chip">
            <span className="pdf">PDF</span>
            <div><strong>보고서 PDF</strong><small>PDF · 최대 30MB</small></div>
            <span className="tag">첨부</span>
          </div>
          <button className="link-btn"><i className="ri-add-line" /> 파일 추가</button>
        </div>

        <div className="chat-input">
          <input placeholder="예) 이 보고서의 매출액과 영업이익을 검증해줘" />
          <button className="btn-icon" onClick={() => navigate("/progress")} aria-label="전송">
            <i className="ri-arrow-up-line" />
          </button>
        </div>

        <p className="muted small mt-lg">자주 쓰는 요청</p>
        <div className="grid3">
          {QUICK_STARTS.map((q) => (
            <button key={q.title} className="card quick" onClick={() => navigate(q.to)}>
              <strong>{q.title}</strong><small>{q.desc}</small>
            </button>
          ))}
        </div>
      </div>
    </AppShell>
  );
}
