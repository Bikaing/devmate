// code_review 接口层：SSE 流式审查 + 任务列表/详情 + HitL apply/reject。
// SSE 与 sse.ts 同套路：fetch + ReadableStream 手动切分（EventSource 不支持 POST）。
import http from "./http";
import { useAuthStore } from "@/stores/auth";
import type { ReviewFinding, ReviewMode, ReviewTask, ReviewTaskDetail } from "./types";

export interface ReviewPayload {
  repo_id: string;
  mode: ReviewMode;
  base_ref?: string;
  head_ref?: string;
  review_basis?: string;
}

export interface ReviewSseHandlers {
  onMeta?: (d: { task_id: string; repo_id: string }) => void;
  onProgress?: (d: Record<string, unknown>) => void;
  onComment?: (d: { file: string; findings: ReviewFinding[] }) => void;
  onDone?: (d: {
    task_id: string;
    findings: ReviewFinding[];
    summary: string;
    duration_ms: number;
  }) => void;
  onError?: (d: { message: string }) => void;
  // 流开始前的 HTTP 错误（400 参数非法 / 401 未鉴权）
  onHttpError?: (status: number, detail: string) => void;
}

export async function streamReview(
  payload: ReviewPayload,
  handlers: ReviewSseHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const auth = useAuthStore();
  let resp: Response;
  try {
    resp = await fetch("/api/v1/code-review/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${auth.token}`,
      },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (e) {
    if ((e as Error).name !== "AbortError") handlers.onHttpError?.(0, "网络请求失败");
    return;
  }

  if (!resp.ok || !resp.body) {
    let detail = resp.statusText;
    try {
      const j = await resp.json();
      detail = j.detail || detail;
    } catch {
      /* 忽略解析失败，用 statusText 兜底 */
    }
    handlers.onHttpError?.(resp.status, detail);
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 事件以空行（\n\n）分隔，逐条切出派发
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      dispatch(raw, handlers);
    }
  }
}

function dispatch(raw: string, handlers: ReviewSseHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;
  let data: unknown;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return;
  }
  switch (event) {
    case "meta":
      handlers.onMeta?.(data as never);
      break;
    case "progress":
      handlers.onProgress?.(data as never);
      break;
    case "comment":
      handlers.onComment?.(data as never);
      break;
    case "done":
      handlers.onDone?.(data as never);
      break;
    case "error":
      handlers.onError?.(data as never);
      break;
  }
}

export async function listReviewTasks(): Promise<ReviewTask[]> {
  const { data } = await http.get<ReviewTask[]>("/code-review/tasks");
  return data;
}

export async function getReviewTask(taskId: string): Promise<ReviewTaskDetail> {
  const { data } = await http.get<ReviewTaskDetail>(`/code-review/tasks/${taskId}`);
  return data;
}

export async function applyFinding(
  findingId: string,
): Promise<{ finding_id: string; hitl_status: string; applied_commit: string }> {
  const { data } = await http.post(`/code-review/findings/${findingId}/apply`);
  return data;
}

export async function rejectFinding(
  findingId: string,
): Promise<{ finding_id: string; hitl_status: string }> {
  const { data } = await http.post(`/code-review/findings/${findingId}/reject`);
  return data;
}
