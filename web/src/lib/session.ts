// 登录状态：启动时问一次 /api/session；任何接口返回 401 都回到登录页

import { api, ApiError, setUnauthorizedHandler } from "./api";
import { queryClient } from "./queries";
import { createStore } from "./store";

export interface Session {
  status: "loading" | "login" | "ready" | "offline";
  required: boolean;
  user: string | null;
}

const store = createStore<Session>({ status: "loading", required: false, user: null });
export const useSession = store.use;

export async function loadSession() {
  try {
    const s = await api.get<{ required: boolean; user: string | null }>("/api/session");
    store.set({ status: s.required && !s.user ? "login" : "ready", required: s.required, user: s.user });
  } catch {
    store.set(prev => ({ ...prev, status: "offline" }));
  }
}

setUnauthorizedHandler(() => {
  if (store.get().status === "ready") store.set(prev => ({ ...prev, status: "login", user: null }));
});

export async function login(username: string, password: string, remember: boolean) {
  const r = await api.post<{ user: string | null }>("/api/login", { username, password, remember });
  queryClient.clear();
  store.set({ status: "ready", required: true, user: r.user });
}

export async function logout() {
  try {
    await api.post("/api/logout");
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
  }
  queryClient.clear();
  store.set(prev => ({ ...prev, status: "login", user: null }));
}
