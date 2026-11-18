import { USE_MOCK, delay, request } from "./client.js";
import { CLAIMS, RECENT_JOBS } from "../data/mock.js";

// TODO(API): 엔드포인트 확정 후 request() 호출로 교체

export const authApi = {
  async login({ email, password }) {
    if (USE_MOCK) { await delay(); return { user: { name: "홍길동", email } }; }
    return request("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
  },
  async signup({ name, email, password }) {
    if (USE_MOCK) { await delay(); return { ok: true }; }
    return request("/auth/signup", { method: "POST", body: JSON.stringify({ name, email, password }) });
  },
};

export const jobsApi = {
  async list() {
    if (USE_MOCK) { await delay(50); return RECENT_JOBS; }
    return request("/jobs");
  },
  async create(/* file, message */) {
    if (USE_MOCK) { await delay(); return { id: "job-1" }; }
    return request("/jobs", { method: "POST" });
  },
};

export const claimsApi = {
  async list(/* jobId */) {
    if (USE_MOCK) { await delay(50); return CLAIMS; }
    return request("/jobs/:id/claims");
  },
  async decide(/* claimId, decision */) {
    if (USE_MOCK) { await delay(50); return { ok: true }; }
    return request("/claims/:id/decision", { method: "PATCH" });
  },
};
