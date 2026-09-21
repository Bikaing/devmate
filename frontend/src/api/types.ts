// 后端接口的 TypeScript 类型契约（与 backend 各 endpoint 的返回体一一对应）。

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  role: string;
  user_id: string;
}

export type IndexStatus = "pending" | "indexing" | "indexed" | "failed";

export interface Repo {
  id: string;
  name: string;
  path: string;
  index_status: IndexStatus;
  last_indexed_at: string | null;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  agent_type: string;
  status: string;
  last_active_at: string;
  created_at: string;
}

// citations 落库/前端契约用 file 字段（retriever 内部叫 path，已由 orchestrator 映射）
export interface Citation {
  file: string;
  start_line: number;
  end_line: number;
  symbol: string | null;
}

export interface HistoryMessage {
  id: string;
  role: "user" | "assistant" | "system" | "tool";
  msg_type: "text" | "diff" | "confirm_card" | "code_block" | "error";
  content: string;
  citations: Citation[] | null;
  web_sources: WebSource[] | null;
  created_at: string;
}

// 联网搜索参考来源（与 citations 仓库引用契约分离）
export interface WebSource {
  title: string;
  href: string;
  body: string;
}

// 前端聊天窗内部使用的消息模型（兼容历史回放与流式增量）
export interface UiMessage {
  id?: string;
  role: "user" | "assistant";
  msgType: "text" | "error";
  content: string;
  citations?: Citation[];
  webSources?: WebSource[];
  streaming?: boolean;
}

// ── code_review 契约（与 backend/api/v1/code_review.py 返回体一一对应）──
export type ReviewMode = "uncommitted" | "range" | "commit";
export type HitlStatus = "pending" | "applied" | "rejected";
export type ReviewStatus = "running" | "success" | "failed";

export interface ReviewDiffStats {
  files?: number;
  added?: number;
  deleted?: number;
  skipped?: { path: string; reason: string }[];
}

export interface ReviewFinding {
  id: string;
  file: string;
  start_line: number;
  end_line: number;
  severity: "critical" | "warning" | "info";
  category: "bug" | "security" | "perf" | "style" | "test";
  title: string;
  suggestion: string;
  suggested_diff: string | null;
  hitl_status: HitlStatus;
  applied_commit: string | null;
  source: "rule" | "llm";
  created_at: string;
}

export interface ReviewTask {
  id: string;
  repo_id: string;
  mode: ReviewMode;
  base_ref: string;
  head_ref: string;
  status: ReviewStatus;
  summary: string | null;
  diff_stats: ReviewDiffStats;
  error_message: string | null;
  duration_ms: number | null;
  created_at: string;
}

// 详情比列表多 review_basis 与全量 findings
export interface ReviewTaskDetail extends ReviewTask {
  review_basis: string | null;
  findings: ReviewFinding[];
}

// ── doc_insight 契约（与 backend/api/v1/doc_insight.py 返回体一一对应）──
export type DocTaskStatus = "running" | "success" | "failed";

// 列表不带 content/report 大字段
export interface DocTask {
  id: string;
  title: string;
  status: DocTaskStatus;
  error_message: string | null;
  duration_ms: number | null;
  created_at: string;
}

// 详情多原文与完整报告（历史回看用）
export interface DocTaskDetail extends DocTask {
  content: string;
  report: string | null;
}
