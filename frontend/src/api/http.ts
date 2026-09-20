import axios from "axios";
import { useAuthStore } from "@/stores/auth";

// baseURL 用相对路径 /api/v1，开发期由 vite 代理转发到后端，免 CORS。
const http = axios.create({ baseURL: "/api/v1", timeout: 30000 });

http.interceptors.request.use((config) => {
  const auth = useAuthStore();
  if (auth.token) config.headers.Authorization = `Bearer ${auth.token}`;
  return config;
});

http.interceptors.response.use(
  (res) => res,
  (err) => {
    const status = err.response?.status;
    // Token 失效/被禁用：清本地登录态并整页跳登录（避免与 router 循环依赖，用 location 硬跳）
    if (status === 401 || status === 403) {
      useAuthStore().clear();
      if (!location.pathname.startsWith("/login")) location.href = "/login";
    }
    return Promise.reject(err);
  },
);

export default http;
