import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { authApi } from "../../api/index.js";
import AuthHero from "./AuthHero.jsx";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
const PW_RE = /^(?=.*[A-Za-z])(?=.*\d).{8,}$/;

export default function SignupPage() {
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: "", email: "", password: "", passwordConfirm: "" });
  const [agreeTerms, setAgreeTerms] = useState(false);
  const [agreePrivacy, setAgreePrivacy] = useState(false);
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  const handleChange = (ev) => {
    const { name, value } = ev.target;
    setForm((p) => ({ ...p, [name]: value }));
    setErrors((p) => ({ ...p, [name]: "" }));
    setServerError("");
  };

  const validate = () => {
    const e = {};
    if (!form.name.trim()) e.name = "이름을 입력해 주세요.";
    if (!form.email.trim()) e.email = "이메일을 입력해 주세요.";
    else if (!EMAIL_RE.test(form.email.trim())) e.email = "올바른 이메일 주소를 입력해 주세요.";
    if (!PW_RE.test(form.password)) e.password = "영문과 숫자를 포함해 8자 이상 입력해 주세요.";
    if (form.password !== form.passwordConfirm) e.passwordConfirm = "비밀번호가 일치하지 않습니다.";
    if (!agreeTerms) e.consent = "서비스 이용약관에 동의해 주세요.";
    else if (!agreePrivacy) e.consent = "개인정보 수집 및 이용에 동의해 주세요.";
    return e;
  };

  const onSubmit = async (ev) => {
    ev.preventDefault();
    const e = validate();
    setErrors(e);
    if (Object.keys(e).length) return;
    setSubmitting(true);
    try {
      await authApi.signup({ name: form.name.trim(), email: form.email.trim(), password: form.password });
      setDone(true);
    } catch (err) {
      setServerError(err.message || "회원가입에 실패했습니다.");
    } finally {
      setSubmitting(false);
    }
  };

  const field = (name, label, type, placeholder, auto) => (
    <div className="form-field">
      <label htmlFor={`su-${name}`}>{label}</label>
      <input
        id={`su-${name}`} name={name} type={type} autoComplete={auto} placeholder={placeholder}
        className={`form-input ${errors[name] ? "is-error" : ""}`}
        value={form[name]} onChange={handleChange}
      />
      {errors[name] && <p className="form-error">{errors[name]}</p>}
    </div>
  );

  return (
    <div className="auth-wrap">
      <AuthHero />

      <main className="auth-page">
        {done ? (
          <div className="auth-card auth-success-card">
            <h2>회원가입이 완료되었습니다.</h2>
            <button className="btn btn-primary btn-block" onClick={() => navigate("/login")}>
              로그인하러 가기
            </button>
          </div>
        ) : (
          <div className="auth-card">
            <div className="auth-intro left">
              <h2>회원가입</h2>
              <p>근거 기반 재무 보고서 검증 시작하기</p>
            </div>
            <form className="stack" onSubmit={onSubmit} noValidate>
              {serverError && <div className="alert-error">{serverError}</div>}
              {field("name", "이름", "text", "이름을 입력해 주세요", "name")}
              {field("email", "이메일 주소", "email", "yourmail@domain.com", "email")}
              {field("password", "비밀번호", "password", "영문, 숫자 포함 8자 이상", "new-password")}
              {field("passwordConfirm", "비밀번호 확인", "password", "비밀번호를 다시 입력해 주세요", "new-password")}

              <div className={`auth-consent-box ${errors.consent ? "is-error" : ""}`}>
                <label className="check">
                  <input type="checkbox" checked={agreeTerms}
                    onChange={(e) => { setAgreeTerms(e.target.checked); setErrors((p) => ({ ...p, consent: "" })); }} />
                  <span>[필수] 서비스 이용약관 동의</span>
                </label>
                <label className="check">
                  <input type="checkbox" checked={agreePrivacy}
                    onChange={(e) => { setAgreePrivacy(e.target.checked); setErrors((p) => ({ ...p, consent: "" })); }} />
                  <span>[필수] 개인정보 수집 및 이용 동의</span>
                </label>
                {errors.consent && <p className="form-error">{errors.consent}</p>}
              </div>

              <button type="submit" className="btn btn-primary btn-block" disabled={submitting}>
                {submitting ? "가입 처리 중..." : "회원가입 완료"}
              </button>
              <p className="auth-switch">
                이미 계정이 있으신가요? <Link to="/login">로그인</Link>
              </p>
            </form>
          </div>
        )}
      </main>
    </div>
  );
}
