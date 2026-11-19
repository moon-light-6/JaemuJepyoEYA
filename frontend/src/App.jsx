import { Navigate, Route, Routes } from "react-router-dom";
import LoginPage from "./pages/auth/LoginPage.jsx";
import SignupPage from "./pages/auth/SignupPage.jsx";
import ChatNewPage from "./pages/ChatNewPage.jsx";
import ChatResultPage from "./pages/ChatResultPage.jsx";
import ChatGeneratePage from "./pages/ChatGeneratePage.jsx";
import ProgressPage from "./pages/ProgressPage.jsx";
import ReviewPage from "./pages/ReviewPage.jsx";
import ExportPage from "./pages/ExportPage.jsx";

// 페이지 이동만 구현. 인증 가드/실제 API 연동은 API 설계 후 추가.
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/login" replace />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/signup" element={<SignupPage />} />
      <Route path="/chat" element={<ChatNewPage />} />
      <Route path="/chat/generate" element={<ChatGeneratePage />} />
      <Route path="/chat/result" element={<ChatResultPage />} />
      <Route path="/progress" element={<ProgressPage />} />
      <Route path="/review" element={<ReviewPage />} />
      <Route path="/export" element={<ExportPage />} />
      <Route path="*" element={<Navigate to="/login" replace />} />
    </Routes>
  );
}
