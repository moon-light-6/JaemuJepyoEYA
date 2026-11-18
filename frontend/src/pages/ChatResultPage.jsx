import { useNavigate } from "react-router-dom";
import AppShell from "../components/AppShell.jsx";

export default function ChatResultPage() {
  const navigate = useNavigate();
  return (
    <AppShell variant="chat" title="2025 재무 분석 보고서" pill="검증 완료" pillClass="pill-green">
      <div className="chat-thread">
        <p className="center muted small">오늘</p>
        <div className="row-user">
          <span className="avatar sm light">SY</span>
          <div className="bubble-user">이 보고서를 전체 검증해줘. 특히 매출액이랑 연결/별도 기준을 봐줘.</div>
        </div>

        <div className="bubble-ai">
          <span className="avatar purple">F</span>
          <div className="ai-body">
            <p className="ai-title">보고서에서 30개의 검증 가능한 주장을 찾았어요.</p>
            <p className="muted small">공시 자료와 비교한 결과, 2개는 오류이고 1개는 기준·기간이 불분명해 보류예요.</p>

            <div className="card file-result">
              <span className="pdf green">PDF</span>
              <div><strong>검증 결과 요약</strong><small>30개 주장 · 근거 18개 연결됨</small></div>
              <button className="tag green" onClick={() => navigate("/review")}>열기</button>
            </div>

            <div className="card claim-card">
              <div className="row between"><strong>우선 확인이 필요한 주장</strong><span className="tag red">오류</span></div>
              <h4>매출액은 100조 원이다.</h4>
              <p className="muted small">공시 금액 120조 원과 불일치 · 별도 · 2025년</p>
              <hr />
              <p className="fix"><b>수정 제안</b> 매출액은 120조 원이다.</p>
              <div className="row gap end">
                <button className="btn btn-ghost" onClick={() => navigate("/review")}>근거 보기</button>
                <button className="btn btn-ghost">정정 제외</button>
                <button className="btn btn-primary" onClick={() => navigate("/export")}>수정 승인</button>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div className="chat-input fixed">
        <input placeholder="이 작업에 대해 질문하거나 다음 작업을 요청하세요" />
        <button className="btn-icon" aria-label="전송"><i className="ri-arrow-up-line" /></button>
      </div>
    </AppShell>
  );
}
