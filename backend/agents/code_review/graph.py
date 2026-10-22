# backend/agents/code_review/graph.py
# code_review：diff -> rules -> context（种子窗口）-> analysis -> review -> summarize。
# diff/analysis 失败写 error 并经条件边结束；context 可降级，
# rules 是普通规则工具；review 逐文件重试一次后明确标记失败，
# summarize 失败退化为统计话术。
# review 节点 get_structured_llm + Semaphore 逐文件并发；进度/结果派发
import asyncio

from langchain_core.callbacks import adispatch_custom_event
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from backend.agents.code_review import prompts
from backend.agents.code_review.analysis.graph import analyze_change
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


async def analysis_node(state: ReviewState) -> dict:
    """独立分析 Agent 按需搜集代码证据，失败明确终止审查。"""
    sem = asyncio.Semaphore(REVIEW_CONCURRENCY)
    files = state.get('diff_files') or []
    contexts = state.get('contexts') or {}

    async def one(index, file):
        async with sem:
            try:
                analysis = await analyze_change(state['repo_path'], state['mode'],
                    state.get('head_ref', ''), file, contexts.get(file['path']) or [],
                    state.get('review_basis', ''))
                await adispatch_custom_event('review_progress', dict(stage='analysis',
                    file=file['path'], done=index + 1, total=len(files),
                    evidence_count=len(analysis['evidence']), status='success'))
                return file['path'], analysis, None
            except Exception as exc:
                logger.exception('code_review.analysis_failed', path=file['path'])
                await adispatch_custom_event('review_progress', dict(stage='analysis',
                    file=file['path'], done=index + 1, total=len(files), status='failed'))
                reason = getattr(exc, 'public_message', type(exc).__name__)
                return file['path'], None, f"文件 {file['path']} 分析失败（{reason}），审查未完成"

    results = await asyncio.gather(*(one(i, file) for i, file in enumerate(files)))
    errors = [error for _, _, error in results if error]
    return dict(analyses={path: result for path, result, _ in results if result},
                error='\n'.join(errors) if errors else None)


def _route_after_analysis(state: ReviewState) -> str:
    return END if state.get('error') else 'review'


async def _review_file(llm, f: dict, contexts: list[dict], basis: str, analysis: dict | None = None) -> list[dict]:
    """单文件审查：调用、解析或结构校验失败重试一次，耗尽后上报失败。"""
    messages = [SystemMessage(content=prompts.SYSTEM_PROMPT),
                HumanMessage(content=prompts.build_user_prompt(f, contexts, basis, analysis))]
    for attempt in (1, 2):
        try:
            out = await llm.ainvoke(messages)
            if not isinstance(out, prompts.ReviewOutputModel):
                raise ValueError("模型未返回有效的审查结构")
            return [{**fm.model_dump(), "source": "llm"} for fm in out.findings]
        except Exception as exc:
            last_error = exc
            logger.exception("code_review.review_file_failed", path=f["path"], attempt=attempt)
    raise RuntimeError(
        f"文件 {f['path']} 审查失败：模型调用或结果解析连续 2 次失败"
        f"（{type(last_error).__name__}）；本次审查未完成，不能据此判断没有问题。"
    ) from last_error


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

    async def one(idx: int, f: dict) -> tuple[list[dict], str | None]:
        async with sem:
            try:
                analysis = (state.get('analyses') or {}).get(f['path'])
                evidence = analysis['evidence'] if analysis else contexts.get(f['path']) or []
                found = await _review_file(llm, f, evidence, basis, analysis)
            except Exception as exc:
                logger.exception("code_review.file_incomplete", path=f["path"])
                await adispatch_custom_event("review_progress", {
                    "stage": "review", "file": f["path"], "done": idx + 1,
                    "total": total, "status": "failed"})
                return [], str(exc)
        await adispatch_custom_event("review_progress", {
            "stage": "review", "file": f["path"], "done": idx + 1, "total": total})
        if found:
            await adispatch_custom_event("review_comment", {"file": f["path"], "findings": found})
        return found, None

    batches = await asyncio.gather(*(one(i, f) for i, f in enumerate(diff_files)))
    llm_findings = [x for found, _ in batches for x in found]
    errors = [error for _, error in batches if error]
    return {"findings": _merge_findings(state.get("rule_findings") or [], llm_findings),
            "error": "\n".join(errors) if errors else None}


async def summarize_node(state: ReviewState) -> dict:
    """摘要节点：空变更走静态话术免 LLM；LLM 失败退化为统计话术。"""
    if state.get("error"):
        return {}
    findings = state.get("findings") or []
    stats = state.get("diff_stats") or {}
    summary_stats = {**stats, 'summary_scope': 'critical'}
    if not stats.get("files") and not findings:
        return {"summary": "本次变更为空，未发现问题。", 'diff_stats': summary_stats}
    if not any(f['severity'] == 'critical' for f in findings):
        await adispatch_custom_event('review_progress', {'stage': 'summarize'})
        return {'summary': prompts.critical_summary(findings), 'diff_stats': summary_stats}
    try:
        llm = get_structured_llm("summarize", prompts.SummaryModel)
        out = await llm.ainvoke([HumanMessage(
            content=prompts.build_summarize_prompt(stats, findings))])
        summary = out.summary
    except Exception:
        logger.exception("code_review.summarize_failed")
        summary = prompts.critical_summary(findings)
    await adispatch_custom_event("review_progress", {"stage": "summarize"})
    return {"summary": summary, 'diff_stats': summary_stats}


def build_code_review_graph(checkpointer=None):
    """分析先于审查；checkpointer 保存分析报告和证据。"""
    graph = StateGraph(ReviewState)
    graph.add_node("diff", diff_node)
    graph.add_node("rules", rules_node)
    graph.add_node("context", context_node)
    graph.add_node("analysis", analysis_node)
    graph.add_node("review", review_node)
    graph.add_node("summarize", summarize_node)
    graph.set_entry_point("diff")
    graph.add_conditional_edges("diff", _route_after_diff)
    graph.add_edge("rules", "context")
    graph.add_edge("context", "analysis")
    graph.add_conditional_edges('analysis', _route_after_analysis)
    graph.add_edge("review", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile(checkpointer=checkpointer)
