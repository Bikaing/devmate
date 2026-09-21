# backend/agents/code_qa/graph.py
# code_qa LangGraph 图：route -> retrieve -> generate 三节点线性图。
# route 用快模型二分类意图（repo/general），general 跳过仓库检索直接答；
# generate 用 astream 流式生成并逐 token 派发 qa_token 自定义事件，SSE 直接监听转发；
# 重试/降级不写在节点里，由 API 层套 retry.with_retry 统一负责。
from dataclasses import asdict

import asyncio

from langchain_core.callbacks import adispatch_custom_event
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from backend.agents.code_qa.prompts import (GENERAL_CONTEXT_HINT,
                                            ROUTE_SYSTEM_PROMPT, SYSTEM_PROMPT,
                                            USER_TEMPLATE, format_contexts,
                                            format_web_sources)
from backend.agents.code_qa.retriever import retrieve
from backend.agents.code_qa.state import CodeQAState
from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger
from backend.mcp import call_mcp_tool

logger = get_logger(__name__)

WEB_MAX_RESULTS = 5  # 联网开关打开时单次搜索条数


async def _web_search(query: str) -> list[dict]:
    """经 MCP 统一出口搜网页；失败降级为空列表（不中断回答，prompt 不占位）。
    注意：降级日志不用 logger.exception——Python 3.11.0 的 traceback 格式化对含中文的
    源码行有 UnicodeDecodeError 缺陷（gh-99585，3.11.1 修复），会把日志调用本身炸穿。"""
    try:
        return await call_mcp_tool("web_search", "web_search",
                                   {"query": query, "max_results": WEB_MAX_RESULTS})
    except Exception as e:  # noqa: BLE001 MCPToolError 等一律降级
        logger.error("code_qa.web_search_failed", query=query[:80],
                     error=f"{type(e).__name__}: {e}"[:500])
        return []


async def route_node(state: CodeQAState) -> dict:
    """意图路由节点：快模型二分类（repo/general）；分类失败安全回落 repo（保持原 RAG 行为）。
    与联网开关正交：开关管搜不搜网页，意图管搜不搜仓库。"""
    try:
        llm = get_llm("route")
        res = await llm.ainvoke([SystemMessage(content=ROUTE_SYSTEM_PROMPT),
                                 HumanMessage(content=state["query"])])
        text = (res.content or "").strip().lower()
        intent = "general" if ("general" in text or "通用" in text) else "repo"
    except Exception as e:  # noqa: BLE001 分类失败回落 RAG 主链路
        logger.error("code_qa.route_failed", query=state["query"][:80],
                     error=f"{type(e).__name__}: {e}"[:300])
        intent = "repo"
    logger.info("code_qa.routed", intent=intent, query=state["query"][:80])
    return {"intent": intent}


async def retrieve_node(state: CodeQAState) -> dict:
    """检索节点：intent=general 时跳过仓库召回（contexts 空）；
    仓库召回失败不中断回答，降级为无上下文分支；
    联网开关 ON 时并行 web 搜索，结果独立存 web_sources（不混入 citations 契约）。"""
    need_repo = state.get("intent") != "general"
    need_web = bool(state.get("web_search_enabled"))
    if need_repo and need_web:
        contexts, web = await asyncio.gather(
            _retrieve_safe(state), _web_search(state["query"]))
    elif need_repo:
        contexts, web = await _retrieve_safe(state), []
    elif need_web:
        contexts, web = [], await _web_search(state["query"])
    else:
        contexts, web = [], []
    return {"contexts": contexts, "web_sources": web}


async def _retrieve_safe(state: CodeQAState) -> list[dict]:
    try:
        contexts = await retrieve(state["repo_id"], state["query"])
    except Exception:
        logger.exception("code_qa.retrieve_failed", repo_id=state.get("repo_id"))
        return []
    return [asdict(c) for c in contexts]


async def generate_node(state: CodeQAState) -> dict:
    """生成节点：流式调用 LLM，逐 token 派发事件；结束写回 answer/citations/messages。"""
    contexts = state.get("contexts") or []
    # general 意图：不喂仓库占位提示，换直答提示；repo 意图空命中仍用 NO_CONTEXT_HINT
    contexts_text = (GENERAL_CONTEXT_HINT if state.get("intent") == "general"
                     else format_contexts(contexts))
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=USER_TEMPLATE.format(
            contexts=contexts_text,
            web_section=format_web_sources(state.get("web_sources") or []),
            query=state["query"])),
    ]
    llm = get_llm("code_qa", streaming=True)
    parts: list[str] = []
    async for chunk in llm.astream(messages):
        if chunk.content:
            parts.append(chunk.content)
            await adispatch_custom_event("qa_token", {"text": chunk.content})
    answer = "".join(parts)
    return {"answer": answer, "citations": _dedup_citations(contexts),
            "messages": [AIMessage(content=answer)]}


def _dedup_citations(contexts: list[dict]) -> list[dict]:
    """跨上下文块按 (path, 起, 止) 去重，保持命中顺序。"""
    seen: set[tuple[str, int, int]] = set()
    out: list[dict] = []
    for ctx in contexts:
        for c in ctx["citations"]:
            key = (c["path"], c["start_line"], c["end_line"])
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out


def build_code_qa_graph(checkpointer=None):
    """编译三节点线性图；checkpointer 由调用方注入（lifespan 用 PG 版，单测用 MemorySaver）。"""
    graph = StateGraph(CodeQAState)
    graph.add_node("route", route_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("generate", generate_node)
    graph.set_entry_point("route")
    graph.add_edge("route", "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", END)
    return graph.compile(checkpointer=checkpointer)
