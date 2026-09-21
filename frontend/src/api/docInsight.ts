// doc_insight 接口层：SSE 流式洞察 + 任务列表/详情。
// SSE 与 codeReview.ts 同套路：fetch + ReadableStream 手动切分（EventSource 不支持 POST）。
import http from "./http";
import { useAuthStore } from "@/stores/auth";
import type { DocTask, DocTaskDetail } from "./types";

export interface DocInsightPayload {
  title: string;
  content: string;
}

export interface DocInsightSseHandlers {
  onMeta?: (d: { task_id: string; title: string }) => void;
  onProgress?: (d: Record<string, unknown>) => void;
  onToken?: (d: { text: string }) => void;
  onDone?: (d: { task_id: string; report: string; duration_ms: number }) => void;
  onError?: (d: { message: string }) => void;
  // 流开始前的 HTTP 错误（400 参数非法 / 401 未鉴权）
  onHttpError?: (status: number, detail: string) => void;
}

export async function streamDocInsight(
  payload: DocInsightPayload,
  handlers: DocInsightSseHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const auth = useAuthStore();
  let resp: Response;
  try {
    resp = await fetch("/api/v1/doc-insight/tasks", {
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

function dispatch(raw: string, handlers: DocInsightSseHandlers): void {
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
    case "token":
      handlers.onToken?.(data as never);
      break;
    case "done":
      handlers.onDone?.(data as never);
      break;
    case "error":
      handlers.onError?.(data as never);
      break;
  }
}

export async function listDocTasks(): Promise<DocTask[]> {
  const { data } = await http.get<DocTask[]>("/doc-insight/tasks");
  return data;
}

export async function getDocTask(taskId: string): Promise<DocTaskDetail> {
  const { data } = await http.get<DocTaskDetail>(`/doc-insight/tasks/${taskId}`);
  return data;
}
