import { useNavigate } from "react-router-dom";
import AppShell from "../components/AppShell.jsx";
import { PROMPT_SUGGESTIONS } from "../data/mock.js";

export default function ChatGeneratePage() {
  const navigate = useNavigate();
  return (
    <AppShell variant="chat" topLeft={<><b>새 작업</b> <span className="muted">/</span> <b>보고서 생성</b></>}
      pill="보안 보관" pillClass="pill-green">
      <div className="center-col wide">
        <p className="eyebrow">REPORT GENERATION</p>
        <h2 className="hero-title">어떤 재무보고서를 만들어드릴까요?</h2>
        <p className="hero-sub">기업과 기간만 말해도 돼요. 공시 자료를 찾아 분석 보고서를 작성합니다.</p>

        <div className="chat-input big">
          <input defaultValue="SK하이닉스의 2026년 상반기 분석 보고서 만들어줘!" />
          <button className="btn btn-primary wide-btn" onClick={() => navigate("/progress")} aria-label="전송">
            <i className="ri-arrow-up-line" />
          </button>
        </div>
        <p className="muted small hint">↳ PDF를 첨부하지 않아도 바로 요청할 수 있어요.</p>

        <h3 className="mt-lg">이런 질문으로 시작해보세요</h3>
        <div className="grid2">
          {PROMPT_SUGGESTIONS.map((p) => (
            <button key={p.title} className="card suggest" onClick={() => navigate("/progress")}>
              <span className="ico"><i className={p.icon} /></span>
              <div><strong>{p.title}</strong><small>{p.desc}</small></div>
              <i className="ri-arrow-right-s-line" />
            </button>
          ))}
        </div>
      </div>
    </AppShell>
  );
}
