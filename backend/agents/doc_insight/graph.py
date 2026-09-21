# backend/agents/doc_insight/graph.py
# doc_insight LangGraph 图：ingest -> map -> reduce 三节点线性图（map-reduce 洞察）。
# ingest 纯函数分块（不调 LLM）；map 并行逐块提炼（单块失败降级跳过）；
# reduce 汇总生成 Markdown 报告，块数超阈值先分批归并（两层 reduce 防超上下文）；
# 报告用 astream 流式生成并逐 token 派发 doc_token 自定义事件，orchestrator 的 SSE 直接监听转发；
# 重试/降级不写在节点里，由编排层统一负责。
import asyncio

from langchain_core.callbacks import adispatch_custom_event
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from backend.agents.doc_insight.prompts import (CHUNK_SYSTEM_PROMPT,
                                                CHUNK_USER_TEMPLATE,
                                                REDUCE_BATCH_SYSTEM_PROMPT,
                                                REDUCE_BATCH_USER_TEMPLATE,
                                                REPORT_SYSTEM_PROMPT,
                                                REPORT_USER_TEMPLATE,
                                                format_notes)
from backend.agents.doc_insight.state import DocInsightState
from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger

logger = get_logger(__name__)

CHUNK_MAX_CHARS = 6000     # 单块字符上限（中英混排约 3-4k token，deepseek-chat 128k 上下文富余）
CHUNK_OVERLAP_CHARS = 200  # 块间重叠字符，防跨块句子/结论被切断
MAP_CONCURRENCY = 5        # map 并行度：限流防打爆 LLM API
REDUCE_BATCH_NOTES = 20    # 块要点超过该数量时先分批归并再出报告


def split_chunks(content: str) -> list[str]:
    """按段落边界把原文切成带重叠的块；短文档（<=上限）单块直出。"""
    content = content.strip()
    if len(content) <= CHUNK_MAX_CHARS:
        return [content]
    # 先按段落聚合：段落不超上限就整段入块，超长段落按上限硬切
    pieces: list[str] = []
    for para in content.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= CHUNK_MAX_CHARS:
            pieces.append(para)
        else:
            pieces.extend(para[i:i + CHUNK_MAX_CHARS]
                          for i in range(0, len(para), CHUNK_MAX_CHARS))
    chunks: list[str] = []
    buf = ""
    for p in pieces:
        if buf and len(buf) + 2 + len(p) > CHUNK_MAX_CHARS:
            chunks.append(buf)
            buf = buf[-CHUNK_OVERLAP_CHARS:] + "\n\n" + p  # 带上尾重叠开新块
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


async def ingest_node(state: DocInsightState) -> dict:
    """分块节点：纯函数，不调 LLM；空文档由 orchestrator 流前校验兜住。"""
    chunks = split_chunks(state["content"])
    logger.info("doc_insight.ingested", title=state["title"][:80], chunks=len(chunks))
    await adispatch_custom_event("doc_progress",
                                 {"stage": "ingest", "chunks": len(chunks)})
    return {"chunks": chunks}


async def _note_one(title: str, index: int, total: int, chunk: str,
                    sem: asyncio.Semaphore) -> dict | None:
    """单块提炼；失败降级返回 None（跳过该块，不中断整个任务）。"""
    async with sem:
        try:
            llm = get_llm("doc_insight")
            res = await llm.ainvoke([
                SystemMessage(content=CHUNK_SYSTEM_PROMPT),
                HumanMessage(content=CHUNK_USER_TEMPLATE.format(
                    title=title, index=index + 1, total=total, chunk=chunk))])
            note = (res.content or "").strip()
            return {"index": index, "note": note} if note else None
        except Exception as e:  # noqa: BLE001 单块失败不拖垮任务
            # 不用 logger.exception：3.11.0 traceback 对含中文源码行有 UnicodeDecodeError 缺陷
            logger.error("doc_insight.chunk_note_failed", index=index,
                         error=f"{type(e).__name__}: {e}"[:300])
            return None


async def map_node(state: DocInsightState) -> dict:
    """并行逐块提炼要点；全部失败才判任务失败（error 交 orchestrator 收尾）。"""
    chunks = state.get("chunks") or []
    sem = asyncio.Semaphore(MAP_CONCURRENCY)
    results = await asyncio.gather(*(
        _note_one(state["title"], i, len(chunks), c, sem)
        for i, c in enumerate(chunks)))
    notes = [n for n in results if n is not None]
    await adispatch_custom_event("doc_progress",
                                 {"stage": "map", "total": len(chunks), "noted": len(notes)})
    if not notes:
        return {"chunk_notes": [], "error": "所有片段要点提炼均失败（LLM 不可用）"}
    if len(notes) < len(chunks):
        logger.warning("doc_insight.partial_notes",
                       total=len(chunks), noted=len(notes))
    return {"chunk_notes": notes}


async def _reduce_notes_batched(title: str, notes: list[dict]) -> str:
    """块要点过多时先分批归并成中间要点文本，再喂最终报告层。"""
    llm = get_llm("doc_insight")
    merged: list[str] = []
    for i in range(0, len(notes), REDUCE_BATCH_NOTES):
        batch = notes[i:i + REDUCE_BATCH_NOTES]
        res = await llm.ainvoke([
            SystemMessage(content=REDUCE_BATCH_SYSTEM_PROMPT),
            HumanMessage(content=REDUCE_BATCH_USER_TEMPLATE.format(
                title=title, notes=format_notes(batch)))])
        merged.append((res.content or "").strip())
        await adispatch_custom_event("doc_progress", {
            "stage": "reduce_batch",
            "batch": i // REDUCE_BATCH_NOTES + 1,
            "batches": (len(notes) + REDUCE_BATCH_NOTES - 1) // REDUCE_BATCH_NOTES})
    return "\n\n".join(f"【第 {j + 1} 部分要点】\n{m}" for j, m in enumerate(merged) if m)


async def reduce_node(state: DocInsightState) -> dict:
    """汇总层：（可选分批归并）-> 流式生成 Markdown 报告，逐 token 派发 doc_token。"""
    if state.get("error"):
        return {}
    notes = state.get("chunk_notes") or []
    if len(notes) > REDUCE_BATCH_NOTES:
        notes_text = await _reduce_notes_batched(state["title"], notes)
    else:
        notes_text = format_notes(notes)
    await adispatch_custom_event("doc_progress", {"stage": "report"})
    messages = [
        SystemMessage(content=REPORT_SYSTEM_PROMPT),
        HumanMessage(content=REPORT_USER_TEMPLATE.format(
            title=state["title"], notes=notes_text))]
    llm = get_llm("doc_insight", streaming=True)
    parts: list[str] = []
    async for chunk in llm.astream(messages):
        if chunk.content:
            parts.append(chunk.content)
            await adispatch_custom_event("doc_token", {"text": chunk.content})
    return {"report": "".join(parts)}


def build_doc_insight_graph(checkpointer=None):
    """编译三节点线性图；checkpointer 由调用方注入（lifespan 用 PG 版，单测用 MemorySaver）。"""
    graph = StateGraph(DocInsightState)
    graph.add_node("ingest", ingest_node)
    graph.add_node("map", map_node)
    graph.add_node("reduce", reduce_node)
    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "map")
    graph.add_edge("map", "reduce")
    graph.add_edge("reduce", END)
    return graph.compile(checkpointer=checkpointer)
