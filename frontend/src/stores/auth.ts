import { defineStore } from "pinia";

interface AuthState {
  token: string;
  userId: string;
  role: string;
  username: string;
}

const KEY = "devmate_auth";

// 登录态持久化到 localStorage，刷新页面/新开标签仍保持登录。
function load(): AuthState {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return JSON.parse(raw) as AuthState;
  } catch {
    /* 脏数据忽略，回落到未登录 */
  }
  return { token: "", userId: "", role: "", username: "" };
}

export const useAuthStore = defineStore("auth", {
  state: (): AuthState => load(),
  getters: {
    isLoggedIn: (s) => !!s.token,
  },
  actions: {
    setAuth(data: AuthState) {
      this.token = data.token;
      this.userId = data.userId;
      this.role = data.role;
      this.username = data.username;
      localStorage.setItem(KEY, JSON.stringify(data));
    },
    clear() {
      this.token = "";
      this.userId = "";
      this.role = "";
      this.username = "";
      localStorage.removeItem(KEY);
    },
  },
});
