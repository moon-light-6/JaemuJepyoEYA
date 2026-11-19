import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { authApi } from "../../api/index.js";
import AuthHero from "./AuthHero.jsx";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export default function LoginPage() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (ev) => {
    ev.preventDefault();
    const e = {};
    if (!email.trim()) e.email = "이메일을 입력해 주세요.";
    else if (!EMAIL_RE.test(email.trim())) e.email = "올바른 이메일 주소를 입력해 주세요.";
    if (!password) e.password = "비밀번호를 입력해 주세요.";
    setErrors(e);
    if (Object.keys(e).length) return;
    setSubmitting(true);
    setServerError("");
    try {
      await authApi.login({ email: email.trim(), password });
      navigate("/chat");
    } catch (err) {
      setServerError(err.message || "로그인에 실패했습니다.");
    } finally {
      setSubmitting(false);
    }
  };

  // TODO(API): Google OAuth 연결 (카카오 OAuth 는 제외)
  return (
    <div className="auth-wrap">
      <AuthHero />
      <main className="auth-page">
        <div className="auth-card">
          <div className="auth-intro left">
            <h2>환영합니다</h2>
            <p>보고서를 안전하게 검증하려면 로그인해 주세요.</p>
          </div>
          <form className="stack" onSubmit={onSubmit} noValidate>
            {serverError && <div className="alert-error">{serverError}</div>}
            <div className="form-field">
              <label htmlFor="login-email">이메일</label>
              <input id="login-email" type="email" autoComplete="email"
                className={`form-input ${errors.email ? "is-error" : ""}`}
                placeholder="name@example.com" value={email}
                onChange={(e) => { setEmail(e.target.value); setErrors((p) => ({ ...p, email: "" })); }} />
              {errors.email && <p className="form-error">{errors.email}</p>}
            </div>
            <div className="form-field">
              <label htmlFor="login-password">비밀번호</label>
              <input id="login-password" type="password" autoComplete="current-password"
                className={`form-input ${errors.password ? "is-error" : ""}`}
                placeholder="••••••••" value={password}
                onChange={(e) => { setPassword(e.target.value); setErrors((p) => ({ ...p, password: "" })); }} />
              {errors.password && <p className="form-error">{errors.password}</p>}
            </div>
            <button type="submit" className="btn btn-primary btn-block" disabled={submitting}>
              {submitting ? "로그인 중..." : "로그인"}
            </button>
            <div className="or">또는</div>
            <button type="button" className="btn btn-google btn-block" onClick={() => navigate("/chat")}>
              <b>G</b><span>Google로 계속하기</span>
            </button>
            <p className="auth-switch">처음이신가요? <Link to="/signup">회원가입</Link></p>
            <p className="auth-terms">회원가입 시 이용약관 및 개인정보 처리방침에 동의하게 됩니다.</p>
          </form>
        </div>
      </main>
    </div>
  );
}
