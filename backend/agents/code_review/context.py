"""code_review 上下文装配：为审查 LLM 提供"变更文件相关代码"，全链路可降级。

两路来源（contexts[path] = [{path, start_line, end_line, content}]）：
1. 自身窗口：读变更文件 head 侧原文（uncommitted 读工作区，range/commit 走 git show），
   小文件给全文、大文件按 hunk ±CONTEXT_WINDOW 切片并合并重叠窗口；行号精确。
2. 跨文件召回：复用 code_qa retriever（Milvus 混合召回+PG 原文+Reranker），query 由变更行拼装；
   召回结果排除变更文件自身；父块原文按 citations 子块区间切片（仅整文件可寻址时切），
   oversized 窗口原文行号不可寻址，宁缺毋错直接丢弃。

降级契约：未索引 / Milvus 挂 / 文件读不到 均只损失对应上下文（logger 记录），不中断审查流；
无上下文时 prompts 侧走 NO_CONTEXT_HINT 分支。
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from backend.agents.code_qa.retriever import retrieve
from backend.core.logger import get_logger

logger = get_logger(__name__)

WHOLE_FILE_MAX_LINES = 300    # 变更文件全文上限：超过则按 hunk 窗口切片
CONTEXT_WINDOW = 15           # 大文件窗口：hunk 区间向两侧扩展的行数
MAX_CONTEXT_FILES = 20        # 装配上下文的变更文件数上限（超出部分仅基于 diff 审查）
CROSS_TOP_PER_FILE = 2        # 每文件跨文件召回保留的父块数
CROSS_BUDGET = 1500           # 跨文件召回的 token 预算（小于 code_qa 默认，审查主料是 diff）
MAX_CITE_RANGES = 3           # 单个召回父块最多切出的子块片段数
QUERY_CHARS = 400             # 召回 query 截断长度
CONCURRENCY = 4               # 逐文件装配并发上限


def _entry(path: str, start_line: int, end_line: int, content: str) -> dict:
    return {"path": path, "start_line": start_line, "end_line": end_line, "content": content}


def _merge_spans(spans: list[list[int]]) -> list[list[int]]:
    """区间按起点排序后合并重叠/相邻者，返回新列表。"""
    merged: list[list[int]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


async def _read_head_file(repo_path: str, mode: str, head_ref: str, rel_path: str) -> str | None:
    """读变更文件 head 侧原文：uncommitted 读工作区，range/commit 走 git show；读不到返回 None。"""
    if mode == "uncommitted":
        def _read() -> str | None:
            try:
                return (Path(repo_path) / rel_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        return await asyncio.to_thread(_read)

    def _show() -> str | None:
        p = subprocess.run(
            ["git", "-c", "core.quotePath=false", "show", f"{head_ref}:{rel_path}"],
            cwd=repo_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        return p.stdout if p.returncode == 0 else None
    return await asyncio.to_thread(_show)


def _self_entries(rel_path: str, content: str, hunks: list[dict]) -> list[dict]:
    """变更文件自身上下文：小文件全文一条；大文件按 hunk 窗口切片合并，行号精确。"""
    lines = content.splitlines()
    n = len(lines)
    if n == 0:
        return []
    if n <= WHOLE_FILE_MAX_LINES:
        return [_entry(rel_path, 1, n, content)]
    spans = [[max(1, int(h["start_line"]) - CONTEXT_WINDOW),
              min(n, int(h["end_line"]) + CONTEXT_WINDOW)]
             for h in hunks] or [[1, min(n, CONTEXT_WINDOW * 2)]]
    return [_entry(rel_path, lo, hi, "\n".join(lines[lo - 1:hi]))
            for lo, hi in _merge_spans(spans)]


def _cross_entries(ctx, changed_paths: set[str]) -> list[dict]:
    """单个召回父块 → 上下文条目：排除变更文件；整文件原文按 citations 切片，窗口原文丢弃。"""
    if ctx.path in changed_paths:
        return []
    lines = ctx.content.splitlines()
    n = len(lines)
    if not ctx.citations or max(c.end_line for c in ctx.citations) > n:
        # oversized 兄弟窗口原文：行号相对窗口不可寻址，宁缺毋错
        logger.info("code_review.context_window_skipped", path=ctx.path, lines=n)
        return []
    spans = _merge_spans([[c.start_line, c.end_line] for c in ctx.citations])[:MAX_CITE_RANGES]
    return [_entry(ctx.path, lo, hi, "\n".join(lines[lo - 1:hi])) for lo, hi in spans]


def _query_for(f: dict) -> str:
    """召回 query：文件路径 + 增删行内容（去行首符号），截断到 QUERY_CHARS。"""
    parts = [f["path"]]
    for h in f.get("hunks") or []:
        for raw in (h.get("text") or "").splitlines():
            if raw[:1] in ("+", "-") and not raw.startswith(("+++ ", "--- ")):
                parts.append(raw[1:].strip())
    return "\n".join(parts)[:QUERY_CHARS]


async def _cross_contexts(repo_id: str, f: dict, changed_paths: set[str]) -> list[dict]:
    """跨文件召回；任何基础设施异常降级为空列表（审查仍可进行）。"""
    try:
        ctxs = await retrieve(repo_id, _query_for(f), budget=CROSS_BUDGET)
    except Exception:
        logger.exception("code_review.context_retrieve_failed", path=f["path"])
        return []
    out: list[dict] = []
    for ctx in ctxs[:CROSS_TOP_PER_FILE]:
        out.extend(_cross_entries(ctx, changed_paths))
    return out


async def build_contexts(repo_id: str, repo_path: str, mode: str,
                         head_ref: str, diff_files: list[dict]) -> dict[str, list[dict]]:
    """逐变更文件装配上下文（自身窗口+跨文件召回），返回 {path: [entries]}；空上下文不入 dict。"""
    changed = {f["path"] for f in diff_files}
    sem = asyncio.Semaphore(CONCURRENCY)

    async def per(f: dict):
        async with sem:
            entries: list[dict] = []
            if not f.get("deleted_file"):  # 删除文件无 head 侧原文，仅保留跨文件召回
                content = await _read_head_file(repo_path, mode, head_ref, f["path"])
                if content is not None:
                    entries.extend(_self_entries(f["path"], content, f.get("hunks") or []))
            entries.extend(await _cross_contexts(repo_id, f, changed))
            return f["path"], entries

    results = await asyncio.gather(
        *(per(f) for f in diff_files[:MAX_CONTEXT_FILES]), return_exceptions=True)
    out: dict[str, list[dict]] = {}
    for r in results:
        if isinstance(r, BaseException):
            logger.error("code_review.context_file_failed", error=repr(r))
            continue
        path, entries = r
        if entries:
            out[path] = entries
    logger.info("code_review.contexts_built", files=len(out))
    return out
