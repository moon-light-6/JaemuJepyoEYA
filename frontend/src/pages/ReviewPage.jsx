import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import AppShell from "../components/AppShell.jsx";
import { claimsApi } from "../api/index.js";

const TAG = { error: "red", hold: "orange", ok: "green" };

export default function ReviewPage() {
  const navigate = useNavigate();
  const [claims, setClaims] = useState([]);
  const [openId, setOpenId] = useState("c1");

  useEffect(() => { claimsApi.list().then(setClaims); }, []);
  const open = claims.find((c) => c.id === openId);

  return (
    <AppShell variant="work" title="검증 결과" pill="검증 완료" pillClass="pill-green">
      <div className="row between page-head">
        <div>
          <h2 className="hero-title left">검증 결과</h2>
          <p className="muted small">중요한 주장만 펼쳐 보고, 근거를 확인한 뒤 수정 여부를 결정하세요.</p>
        </div>
        <div className="row gap">
          <span className="tag red">오류 2</span><span className="tag orange">보류 1</span><span className="tag green">정상 2</span>
        </div>
      </div>

      <div className="review">
        <section className="card">
          <div className="row between"><h4>검증이 필요한 주장</h4><small className="muted">{claims.length}개</small></div>
          <ul className="rv-list">
            {claims.map((c) => c.reported ? (
              <li key={c.id} className="rv-open">
                <div className="row between">
                  <div className="row gap"><span className={`tag ${TAG[c.status]}`}>{c.label}</span><strong className="rv-claim">{c.claim}</strong></div>
                  <small className="muted">{c.loc}</small>
                </div>
                <hr />
                <div className="row gap amt">
                  <small className="muted">공시 금액</small>
                  <b className="red-t">{c.reported}</b><span>→</span><b>{c.disclosed}</b>
                  <span className="tag red ml">{c.match}</span>
                </div>
                <p className="fix"><b>수정 제안</b> {c.fix}</p>
                <small className="muted">{c.source}</small>
                <div className="row gap end">
                  <button className="btn btn-ghost sm">근거 보기</button>
                  <button className="btn btn-ghost sm">제외</button>
                  <button className="btn btn-primary sm" onClick={() => navigate("/export")}>수정 승인</button>
                </div>
              </li>
            ) : (
              <li key={c.id} className="rv-row" onClick={() => setOpenId(c.id)}>
                <span className={`tag ${TAG[c.status]}`}>{c.label}</span>
                <div><strong>{c.claim}</strong><small>{c.sub || c.loc}</small></div>
                <i className="ri-arrow-right-s-line" />
              </li>
            ))}
          </ul>
        </section>

        <aside className="card preview">
          <div className="row between"><h4>보고서 미리보기</h4><span className="tag">p. {open?.page ?? 2}</span></div>
          <p className="muted small">선택한 주장 위치</p>
          <div className="page-mock">
            <b>2025 재무 분석 보고서</b>
            <hr />
            <h5>2. 수익성 분석</h5>
            <p>2025년 매출액과 영업이익은 전년 대비 개선된 흐름을 보였다.</p>
            <div className="hl-error"><b>매출액은 100조 원이다.</b><small>← 선택한 주장</small></div>
            <p className="faint">이 수치는 공시 원문과 대조하여 검증 결과에서 수정 여부를 결정합니다.</p>
            <hr />
            <b>표 2. 주요 재무지표</b>
          </div>
          <p className="center muted small mt">페이지 {open?.page ?? 2} / 18</p>
          <button className="btn btn-ghost btn-block">원문 전체 보기</button>
        </aside>
      </div>
    </AppShell>
  );
}
