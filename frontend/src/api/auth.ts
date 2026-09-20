import http from "./http";
import type { TokenResponse } from "./types";

export async function login(username: string, password: string): Promise<TokenResponse> {
  const { data } = await http.post<TokenResponse>("/auth/login", { username, password });
  return data;
}

// 注册即登录：后端直接返回 Token，前端无需再调一次 login
export async function register(username: string, password: string): Promise<TokenResponse> {
  const { data } = await http.post<TokenResponse>("/auth/register", { username, password });
  return data;
}

export async function fetchMe(): Promise<{ user_id: string; role: string }> {
  const { data } = await http.get("/auth/me");
  return data;
}
