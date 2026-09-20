# backend/agents/code_review/graph.py
# code_review LangGraph 图：diff -> rules -> context -> review -> summarize 五节点线性图。
# diff 失败写 error 并经条件边直达 END（审查无从谈起）；其余节点各自降级不中断：
# rules 纯函数无失败面，context 内部已降级，review 逐文件重试一次再降级为空列表，
# summarize 失败退化为统计话术。
# review 节点 get_structured_llm + Semaphore 逐文件并发；进度/结果派发
import asyncio

from langchain_core.callbacks import adispatch_custom_event
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from backend.agents.code_review import prompts
from backend.agents.code_review.context import build_contexts
from backend.agents.code_review.diff_parser import collect_diff
from backend.agents.code_review.rules import check_rules
from backend.agents.code_review.state import ReviewState
from backend.core.llm_factory import get_structured_llm
from backend.core.logger import get_logger

logger = get_logger(__name__)

REVIEW_CONCURRENCY = 3  # 逐文件审查并发上限（结构化输出较重，控并发保稳定）


async def diff_node(state: ReviewState) -> dict:
    """diff 节点：三模式取变更；失败写 error 供条件边短路，统计置空兜底。"""
    try:
        out = await collect_diff(state["repo_path"], state["mode"],
                                 state.get("base_ref", ""), state.get("head_ref", ""))
    except Exception as e:
        logger.exception("code_review.diff_failed", repo_id=state.get("repo_id"))
        return {"diff_files": [], "diff_stats": {"files": 0, "added": 0, "deleted": 0, "skipped": []},
                "error": str(e)}
    await adispatch_custom_event("review_progress", {"stage": "diff", **out["diff_stats"]})
    return out


def _route_after_diff(state: ReviewState) -> str:
    """diff 失败（含仓库/参数非法）直接结束；orchestrator 依 error 发 error 事件。"""
    return END if state.get("error") else "rules"


async def rules_node(state: ReviewState) -> dict:
    """规则预检节点：纯函数产出 rule 类 findings。"""
    fs = check_rules(state.get("diff_files") or [], state.get("diff_stats") or {})
    await adispatch_custom_event("review_progress", {"stage": "rules", "count": len(fs)})
    return {"rule_findings": fs}


async def context_node(state: ReviewState) -> dict:
    """上下文装配节点：自身窗口+跨文件召回，内部已全链路降级。"""
    contexts = await build_contexts(
        state["repo_id"], state["repo_path"], state["mode"],
        state.get("head_ref", ""), state.get("diff_files") or [])
    await adispatch_custom_event("review_progress", {"stage": "context", "count": len(contexts)})
    return {"contexts": contexts}


async def _review_file(llm, f: dict, contexts: list[dict], basis: str) -> list[dict]:
    """单文件审查：结构化调用，解析/调用失败重试一次，再失败降级为空列表。"""
    messages = [SystemMessage(content=prompts.SYSTEM_PROMPT),
                HumanMessage(content=prompts.build_user_prompt(f, contexts, basis))]
    for attempt in (1, 2):
        try:
            out = await llm.ainvoke(messages)
            return [{**fm.model_dump(), "source": "llm"} for fm in out.findings]
        except Exception:
            logger.exception("code_review.review_file_failed", path=f["path"], attempt=attempt)
    return []


def _merge_findings(rule: list[dict], llm: list[dict]) -> list[dict]:
    """rule 优先、llm 补充：按 (file, 起, 止, category) 去重，避免密钥等问题双报。"""
    seen = {(f["file"], f["start_line"], f["end_line"], f["category"]) for f in rule}
    out = list(rule)
    for f in llm:
        key = (f["file"], f["start_line"], f["end_line"], f["category"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


async def review_node(state: ReviewState) -> dict:
    """审查节点：Semaphore 控并发逐文件结构化审查；逐文件派发进度与 comment 事件。"""
    diff_files = state.get("diff_files") or []
    if not diff_files:
        return {"findings": list(state.get("rule_findings") or [])}
    contexts = state.get("contexts") or {}
    basis = state.get("review_basis", "")
    llm = get_structured_llm("code_review", prompts.ReviewOutputModel)
    sem = asyncio.Semaphore(REVIEW_CONCURRENCY)
    total = len(diff_files)

    async def one(idx: int, f: dict) -> list[dict]:
        async with sem:
            found = await _review_file(llm, f, contexts.get(f["path"]) or [], basis)
        await adispatch_custom_event("review_progress", {
            "stage": "review", "file": f["path"], "done": idx + 1, "total": total})
        if found:
            await adispatch_custom_event("review_comment", {"file": f["path"], "findings": found})
        return found

    batches = await asyncio.gather(*(one(i, f) for i, f in enumerate(diff_files)))
    llm_findings = [x for b in batches for x in b]
    return {"findings": _merge_findings(state.get("rule_findings") or [], llm_findings)}


async def summarize_node(state: ReviewState) -> dict:
    """摘要节点：空变更走静态话术免 LLM；LLM 失败退化为统计话术。"""
    if state.get("error"):
        return {}
    findings = state.get("findings") or []
    stats = state.get("diff_stats") or {}
    if not stats.get("files") and not findings:
        return {"summary": "本次变更为空，未发现问题。"}
    try:
        llm = get_structured_llm("summarize", prompts.SummaryModel)
        out = await llm.ainvoke([HumanMessage(
            content=prompts.build_summarize_prompt(stats, findings))])
        summary = out.summary
    except Exception:
        logger.exception("code_review.summarize_failed")
        summary = (f"审查完成：{stats.get('files', 0)} 个文件变更，"
                   f"发现 {len(findings)} 个问题，详见问题列表。")
    await adispatch_custom_event("review_progress", {"stage": "summarize"})
    return {"summary": summary}


def build_code_review_graph(checkpointer=None):
    """编译五节点线性图；checkpointer 由调用方注入（lifespan 用 PG 版，单测用 MemorySaver）。"""
    graph = StateGraph(ReviewState)
    graph.add_node("diff", diff_node)
    graph.add_node("rules", rules_node)
    graph.add_node("context", context_node)
    graph.add_node("review", review_node)
    graph.add_node("summarize", summarize_node)
    graph.set_entry_point("diff")
    graph.add_conditional_edges("diff", _route_after_diff)
    graph.add_edge("rules", "context")
    graph.add_edge("context", "review")
    graph.add_edge("review", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile(checkpointer=checkpointer)
