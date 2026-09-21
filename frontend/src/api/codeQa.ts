import http from "./http";
import type { Conversation, HistoryMessage, Repo } from "./types";

export async function listRepos(): Promise<Repo[]> {
  const { data } = await http.get<Repo[]>("/repos");
  return data;
}

// 登记本地仓库（index_status 初始 pending）
export async function registerRepo(path: string, name = ""): Promise<Repo> {
  const { data } = await http.post<Repo>("/repos", { path, name });
  return data;
}

// 触发后台索引，立即返回 indexing，前端轮询 listRepos 看状态变化
export async function triggerIndex(repoId: string): Promise<{ id: string; index_status: string }> {
  const { data } = await http.post(`/repos/${repoId}/index`);
  return data;
}

export async function listConversations(status = "active"): Promise<Conversation[]> {
  const { data } = await http.get<Conversation[]>("/code-qa/conversations", {
    params: { status },
  });
  return data;
}

// 软删除会话（后端置 status=archived，列表默认只查 active）
export async function deleteConversation(conversationId: string): Promise<void> {
  await http.delete(`/code-qa/conversations/${conversationId}`);
}

export async function listMessages(conversationId: string): Promise<HistoryMessage[]> {
  const { data } = await http.get<HistoryMessage[]>(
    `/code-qa/conversations/${conversationId}/messages`,
  );
  return data;
}
