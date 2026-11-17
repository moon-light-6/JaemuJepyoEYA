import { NavLink, useNavigate } from "react-router-dom";
import { RECENT_JOBS } from "../data/mock.js";

// variant="chat": 어두운 사이드바(채팅형) / variant="work": 밝은 사이드바(작업형)
export default function AppShell({ variant = "work", title, pill, pillClass = "pill-blue", children, topLeft }) {
  const navigate = useNavigate();
  const dark = variant === "chat";
  return (
    <div className={`shell ${dark ? "shell-dark" : "shell-light"}`}>
      <aside className="sidebar">
        {dark ? (
          <>
            <button className="side-new" onClick={() => navigate("/chat/generate")}><i className="ri-add-line" /> 새 검증</button>
            <div className="side-section">최근 작업</div>
            <ul className="side-jobs">
              {RECENT_JOBS.map((j, i) => (
                <li key={j.id} className={i === 0 ? "active" : ""} onClick={() => navigate(i === 0 ? "/chat/result" : "/progress")}>
                  <i className={i === 0 ? "ri-stop-circle-line" : "ri-checkbox-blank-circle-line"} />
                  <div><strong>{j.title}</strong><small>{j.sub}</small></div>
                </li>
              ))}
            </ul>
            <div className="side-user" onClick={() => navigate("/login")} title="로그인 화면으로">
              <span className="avatar">SY</span>
              <div><strong>승연 이</strong><small>무료 플랜 · 설정</small></div>
            </div>
          </>
        ) : (
          <>
            <div className="side-logo">
              <span className="logo-box"><i className="ri-check-line" /></span>
              <div><strong>JJEYA</strong><small>재무 보고서 검증</small></div>
            </div>
            <nav className="side-nav">
              <NavLink to="/progress"><i className="ri-home-4-line" />작업 홈</NavLink>
              <NavLink to="/review"><i className="ri-focus-3-line" />검증 결과</NavLink>
              <NavLink to="/export"><i className="ri-arrow-right-up-line" />출력 센터</NavLink>
            </nav>
            <div className="side-line" />
            <div className="side-section">현재 작업</div>
            <div className="job-card">
              <strong>2025 재무 분석 보고서</strong>
              <small>report_2025.pdf · PDF</small>
              <span className="chip-soft">임시 보관</span>
            </div>
            <div className="side-expire">만료 11월 14일 21:00</div>
          </>
        )}
      </aside>
      <section className="main">
        <header className="topbar">
          <div className="crumbs">{topLeft || title}</div>
          {pill && <span className={pillClass}>{pill}</span>}
          <span className={dark ? "avatar sm light" : "avatar-text"}>MY</span>
        </header>
        <div className="content">{children}</div>
      </section>
    </div>
  );
}
