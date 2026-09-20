# backend/agents/code_qa/graph.py
# code_qa LangGraph 图：retrieve -> generate 两节点线性图。
# generate 用 astream 流式生成并逐 token 派发 qa_token 自定义事件，阶段 8 的 SSE 直接监听转发；
# 重试/降级不写在节点里，由 API 层套 retry.with_retry 统一负责。
from dataclasses import asdict

from langchain_core.callbacks import adispatch_custom_event
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from backend.agents.code_qa.prompts import SYSTEM_PROMPT, USER_TEMPLATE, format_contexts
from backend.agents.code_qa.retriever import retrieve
from backend.agents.code_qa.state import CodeQAState
from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger

logger = get_logger(__name__)


async def retrieve_node(state: CodeQAState) -> dict:
    """检索节点：失败不中断回答，降级为无上下文分支（提示词内有对应话术）。"""
    try:
        contexts = await retrieve(state["repo_id"], state["query"])
    except Exception:
        logger.exception("code_qa.retrieve_failed", repo_id=state.get("repo_id"))
        return {"contexts": []}
    return {"contexts": [asdict(c) for c in contexts]}


async def generate_node(state: CodeQAState) -> dict:
    """生成节点：流式调用 LLM，逐 token 派发事件；结束写回 answer/citations/messages。"""
    contexts = state.get("contexts") or []
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=USER_TEMPLATE.format(
            contexts=format_contexts(contexts), query=state["query"])),
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
    """编译两节点线性图；checkpointer 由调用方注入（lifespan 用 PG 版，单测用 MemorySaver）。"""
    graph = StateGraph(CodeQAState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("generate", generate_node)
    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", END)
    return graph.compile(checkpointer=checkpointer)
