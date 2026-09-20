import { useAuthStore } from "@/stores/auth";
import type { Citation } from "./types";

export interface ChatPayload {
  repo_id: string;
  query: string;
  conversation_id?: string | null;
}

export interface SseHandlers {
  onMeta?: (d: { conversation_id: string; run_id: string }) => void;
  onToken?: (d: { text: string }) => void;
  onDone?: (d: { message_id: string; citations: Citation[]; duration_ms: number }) => void;
  onError?: (d: { message: string }) => void;
  // 流开始前的 HTTP 错误（400 输入非法 / 409 索引未就绪 / 401 未鉴权）
  onHttpError?: (status: number, detail: string) => void;
}

// SSE 用 fetch + ReadableStream 手动解析：EventSource 不支持 POST，无法带 body。
export async function streamChat(
  payload: ChatPayload,
  handlers: SseHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const auth = useAuthStore();
  let resp: Response;
  try {
    resp = await fetch("/api/v1/code-qa/chat", {
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

function dispatch(raw: string, handlers: SseHandlers): void {
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
