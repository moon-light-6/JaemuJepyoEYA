// API 구멍(스텁). 서버/DB 설계 전이라 실제 요청은 보내지 않고 목 데이터를 반환합니다.
// 서버가 정해지면 USE_MOCK 을 false 로 바꾸고 각 함수의 request() 부분만 채우면 됩니다.
export const BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";
export const USE_MOCK = true;

export async function request(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}

export const delay = (ms = 200) => new Promise((r) => setTimeout(r, ms));
