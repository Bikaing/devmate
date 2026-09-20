# backend/services/orchestrator.py
# code_qa / code_review 编排层：DB 簿记（会话/消息/agent_runs 或 review_tasks/findings）+
# 图流式执行 + SSE 事件产出。
# 分层职责：endpoint 只做 HTTP/SSE 格式化；本模块只做业务编排；图节点只管推理。
# 重试策略：token 尚未吐出前可安全重试（复用 retry.py 的常量）；
# 一旦已有 token 送达前端，重试会造成重复输出，直接走 Agent 级降级。
import asyncio
import json
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from backend.agents.code_review.applier import apply_finding_patch
from backend.core.exceptions import (InvalidInputError, RepoNotIndexedError,
                                     ReviewHitlStateError)
from backend.core.logger import get_logger
from backend.core.memory import make_config
from backend.core.retry import RETRY_DELAYS, AgentFallbackHandler
from backend.dependencies import AsyncSessionLocal

logger = get_logger(__name__)

INPUT_SUMMARY_LEN = 200   # agent_runs.input_summary 截断长度
TITLE_LEN = 30            # 新建会话标题截断长度


@dataclass
class QARunContext:
    """prepare 阶段产出的簿记上下文，stream 阶段凭它写回结果。"""
    conversation_id: str
    thread_id: str
    run_id: str
    repo_id: str


async def prepare_code_qa(user_id: str, repo_id: str,
                          conversation_id: str | None, query: str) -> QARunContext:
    """流开始前的全部校验与落库：仓库权限/索引状态、会话获取或创建、
    用户消息、agent_run 记录。失败抛业务异常（endpoint 映射 HTTP 码），
    此时 SSE 尚未开始，可以正常返回错误响应。"""
    async with AsyncSessionLocal() as session:
        # 1. 仓库归属 + 索引就绪校验（隔离轴 repo_id 的权限落地）
        repo = (await session.execute(text(
            "SELECT index_status FROM repos WHERE id = :rid AND user_id = :uid"),
            {"rid": uuid.UUID(repo_id), "uid": uuid.UUID(user_id)})).first()
        if repo is None:
            raise InvalidInputError("仓库不存在或无权访问", agent_type="code_qa")
        if repo.index_status != "indexed":
            raise RepoNotIndexedError(
                f"仓库索引未就绪（当前 {repo.index_status}），请先完成索引", agent_type="code_qa")

        # 2. 会话：给了 id 就校验归属与状态，没给就新建（thread_id 即 checkpoint 线程）
        if conversation_id:
            conv = (await session.execute(text(
                "SELECT thread_id, status FROM conversations "
                "WHERE id = :cid AND user_id = :uid"),
                {"cid": uuid.UUID(conversation_id), "uid": uuid.UUID(user_id)})).first()
            if conv is None:
                raise InvalidInputError("会话不存在或无权访问", agent_type="code_qa")
            if conv.status != "active":
                raise InvalidInputError("会话已归档，拒绝追加新消息", agent_type="code_qa")
            thread_id = conv.thread_id
        else:
            thread_id = uuid.uuid4().hex
            conversation_id = str((await session.execute(text(
                "INSERT INTO conversations (user_id, title, agent_type, thread_id) "
                "VALUES (:uid, :title, 'code_qa', :tid) RETURNING id"),
                {"uid": uuid.UUID(user_id), "title": query[:TITLE_LEN], "tid": thread_id}
            )).scalar_one())

        # 3. 用户消息落库（只追加流水，状态恢复由 checkpoint 负责）
        await session.execute(text(
            "INSERT INTO messages (conversation_id, role, msg_type, content) "
            "VALUES (:cid, 'user', 'text', :q)"),
            {"cid": uuid.UUID(conversation_id), "q": query})

        # 4. agent_run 开跑记录（观测三件套的载体：失败能查/成本能算/性能能量）
        run_id = str((await session.execute(text(
            "INSERT INTO agent_runs (conversation_id, repo_id, agent_type, status, input_summary) "
            "VALUES (:cid, :rid, 'code_qa', 'running', :summary) RETURNING id"),
            {"cid": uuid.UUID(conversation_id), "rid": uuid.UUID(repo_id),
             "summary": query[:INPUT_SUMMARY_LEN]})).scalar_one())

        await session.execute(text(
            "UPDATE conversations SET last_active_at = NOW() WHERE id = :cid"),
            {"cid": uuid.UUID(conversation_id)})
        await session.commit()
    return QARunContext(conversation_id, thread_id, run_id, repo_id)


async def stream_code_qa(graph, ctx: QARunContext, query: str):
    """执行图并产出 SSE 事件字典：meta -> token* -> done / error。
    簿记（assistant 消息、run 收尾）在本函数内完成，endpoint 只管格式化转发。"""
    yield {"event": "meta",
           "data": {"conversation_id": ctx.conversation_id, "run_id": ctx.run_id}}
    cfg = make_config(ctx.thread_id)
    graph_input = {"repo_id": ctx.repo_id, "query": query,
                   "messages": [("human", query)]}
    started = time.perf_counter()
    parts: list[str] = []
    failure: Exception | None = None

    # ── 流式执行：token 未吐出前可重试，吐出后失败直接降级 ──
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            async for ev in graph.astream_events(graph_input, config=cfg, version="v2"):
                if ev["event"] == "on_custom_event" and ev["name"] == "qa_token":
                    parts.append(ev["data"]["text"])
                    yield {"event": "token", "data": {"text": ev["data"]["text"]}}
            failure = None
            break
        except Exception as e:  # noqa: BLE001 - 流内任何异常统一进降级决策
            failure = e
            if parts or attempt >= len(RETRY_DELAYS):
                break  # 已有输出不可重试（会重复）；或重试次数耗尽
            logger.warning("orchestrator.stream_retry",
                           attempt=attempt + 1, error=str(e))
            await asyncio.sleep(RETRY_DELAYS[attempt])

    duration_ms = int((time.perf_counter() - started) * 1000)
    async with AsyncSessionLocal() as session:
        if failure is None:
            # ── 成功收尾：从 checkpoint 状态取 answer/citations 落库 ──
            state = (await graph.aget_state(cfg)).values
            answer = state.get("answer") or "".join(parts)
            # citations 契约：DB/前端用 file 字段（retriever 内部叫 path）
            citations = [{"file": c["path"], "start_line": c["start_line"],
                          "end_line": c["end_line"], "symbol": c.get("symbol")}
                         for c in state.get("citations") or []]
            message_id = str((await session.execute(text(
                "INSERT INTO messages (conversation_id, agent_run_id, role, msg_type, "
                "content, citations) VALUES (:cid, :run, 'assistant', 'text', :ans, :cites) "
                "RETURNING id"),
                {"cid": uuid.UUID(ctx.conversation_id), "run": uuid.UUID(ctx.run_id),
                 "ans": answer, "cites": json.dumps(citations, ensure_ascii=False)}
            )).scalar_one())
            await session.execute(text(
                "UPDATE agent_runs SET status = 'success', duration_ms = :dur, "
                "finished_at = NOW() WHERE id = :run"),
                {"dur": duration_ms, "run": uuid.UUID(ctx.run_id)})
            await session.commit()
            yield {"event": "done",
                   "data": {"message_id": message_id, "citations": citations,
                            "duration_ms": duration_ms}}
        else:
            # ── 失败收尾：Agent 级降级话术落库为 error 卡片 ──
            logger.error("orchestrator.stream_failed",
                         run_id=ctx.run_id, error=str(failure))
            fallback = await AgentFallbackHandler.handle("code_qa", failure)
            content = fallback["content"]
            await session.execute(text(
                "INSERT INTO messages (conversation_id, agent_run_id, role, msg_type, content) "
                "VALUES (:cid, :run, 'assistant', 'error', :content)"),
                {"cid": uuid.UUID(ctx.conversation_id), "run": uuid.UUID(ctx.run_id),
                 "content": content})
            await session.execute(text(
                "UPDATE agent_runs SET status = 'failed', duration_ms = :dur, "
                "error_message = :err, finished_at = NOW() WHERE id = :run"),
                {"dur": duration_ms, "err": f"{type(failure).__name__}: {failure}"[:2000],
                 "run": uuid.UUID(ctx.run_id)})
            await session.commit()
            yield {"event": "error", "data": {"message": content}}


# ───────────────────────── code_review 编排 ─────────────────────────
# 簿记载体是 review_tasks/review_findings 两表（不进 conversations/messages/agent_runs）：
# 审查是单发任务无多轮会话，任务级观测字段（status/duration_ms/error_message）表内自带。
REVIEW_MODES = ("uncommitted", "range", "commit")


@dataclass
class ReviewRunContext:
    """prepare_review 阶段产出的簿记上下文，stream_review 凭它执行图与写回。"""
    task_id: str
    repo_id: str
    repo_path: str
    mode: str
    base_ref: str
    head_ref: str
    review_basis: str


async def prepare_review(user_id: str, repo_id: str, mode: str, base_ref: str = "",
                         head_ref: str = "", review_basis: str = "") -> ReviewRunContext:
    """流开始前全部校验与落库：mode/ref 组合、仓库归属、建 review_tasks(running)。
    失败抛业务异常（endpoint 映射 HTTP 码），此时 SSE 尚未开始。
    不校验 index_status：审查主料是 git diff，未索引时跨文件召回自动降级。"""
    if mode not in REVIEW_MODES:
        raise InvalidInputError(f"非法审查模式: {mode}", agent_type="code_review")
    if mode == "range" and not (base_ref.strip() and head_ref.strip()):
        raise InvalidInputError("range 模式需同时提供 base_ref 与 head_ref", agent_type="code_review")
    if mode == "commit" and not head_ref.strip():
        raise InvalidInputError("commit 模式需提供 head_ref", agent_type="code_review")

    async with AsyncSessionLocal() as session:
        repo = (await session.execute(text(
            "SELECT path FROM repos WHERE id = :rid AND user_id = :uid"),
            {"rid": uuid.UUID(repo_id), "uid": uuid.UUID(user_id)})).first()
        if repo is None:
            raise InvalidInputError("仓库不存在或无权访问", agent_type="code_review")
        task_id = str((await session.execute(text(
            "INSERT INTO review_tasks (repo_id, user_id, mode, base_ref, head_ref, "
            "review_basis, status) VALUES (:rid, :uid, :mode, :base, :head, :basis, 'running') "
            "RETURNING id"),
            {"rid": uuid.UUID(repo_id), "uid": uuid.UUID(user_id), "mode": mode,
             "base": base_ref, "head": head_ref, "basis": review_basis})).scalar_one())
        await session.commit()
    return ReviewRunContext(task_id=task_id, repo_id=repo_id, repo_path=repo.path,
                            mode=mode, base_ref=base_ref, head_ref=head_ref,
                            review_basis=review_basis)


async def stream_review(graph, ctx: ReviewRunContext):
    """执行审查图并产出 SSE 事件字典：meta -> progress*/comment* -> done / error。
    簿记（review_tasks 写回 + review_findings 落库）在本函数内完成；
    图内节点已各自降级，流级异常才走 AgentFallbackHandler。"""
    yield {"event": "meta", "data": {"task_id": ctx.task_id, "repo_id": ctx.repo_id}}
    cfg = make_config(ctx.task_id)  # 单发任务：thread_id 取 task id，checkpoint 仅供排障回看
    graph_input = {"repo_id": ctx.repo_id, "repo_path": ctx.repo_path, "mode": ctx.mode,
                   "base_ref": ctx.base_ref, "head_ref": ctx.head_ref,
                   "review_basis": ctx.review_basis}
    started = time.perf_counter()
    failure: Exception | None = None
    try:
        async for ev in graph.astream_events(graph_input, config=cfg, version="v2"):
            if ev["event"] != "on_custom_event":
                continue
            if ev["name"] == "review_progress":
                yield {"event": "progress", "data": ev["data"]}
            elif ev["name"] == "review_comment":
                yield {"event": "comment", "data": ev["data"]}
    except Exception as e:  # noqa: BLE001 - 流内任何异常统一进失败收尾
        failure = e
    duration_ms = int((time.perf_counter() - started) * 1000)

    async with AsyncSessionLocal() as session:
        if failure is not None:
            logger.error("orchestrator.review_stream_failed",
                         task_id=ctx.task_id, error=str(failure))
            fallback = await AgentFallbackHandler.handle("code_review", failure)
            # code_review 降级话术在 fallback_note 键（与其他 Agent 的 content 键不同）
            content = (fallback.get("content") or fallback.get("fallback_note")
                       or "代码审查服务暂时不可用，请稍后重试。")
            await session.execute(text(
                "UPDATE review_tasks SET status = 'failed', error_message = :err, "
                "duration_ms = :dur WHERE id = :tid"),
                {"err": f"{type(failure).__name__}: {failure}"[:2000],
                 "dur": duration_ms, "tid": uuid.UUID(ctx.task_id)})
            await session.commit()
            yield {"event": "error", "data": {"message": content}}
            return

        state = (await graph.aget_state(cfg)).values
        task_error = state.get("error")
        if task_error:  # diff 节点失败：图正常结束但任务判失败
            await session.execute(text(
                "UPDATE review_tasks SET status = 'failed', error_message = :err, "
                "duration_ms = :dur WHERE id = :tid"),
                {"err": task_error[:2000], "dur": duration_ms,
                 "tid": uuid.UUID(ctx.task_id)})
            await session.commit()
            yield {"event": "error", "data": {"message": task_error}}
            return

        findings = state.get("findings") or []
        payload = []
        for f in findings:
            fid = str((await session.execute(text(
                "INSERT INTO review_findings (task_id, file, start_line, end_line, severity, "
                "category, title, suggestion, suggested_diff, source) "
                "VALUES (:tid, :file, :s, :e, :sev, :cat, :title, :sug, :sdiff, :src) "
                "RETURNING id"),
                {"tid": uuid.UUID(ctx.task_id), "file": f["file"], "s": f["start_line"],
                 "e": f["end_line"], "sev": f["severity"], "cat": f["category"],
                 "title": f["title"], "sug": f["suggestion"],
                 "sdiff": f.get("suggested_diff"), "src": f["source"]})).scalar_one())
            payload.append({**f, "id": fid, "hitl_status": "pending"})
        await session.execute(text(
            "UPDATE review_tasks SET status = 'success', summary = :sum, "
            "diff_stats = :stats, duration_ms = :dur WHERE id = :tid"),
            {"sum": state.get("summary") or "", "dur": duration_ms,
             "stats": json.dumps(state.get("diff_stats") or {}, ensure_ascii=False),
             "tid": uuid.UUID(ctx.task_id)})
        await session.commit()
        yield {"event": "done",
               "data": {"task_id": ctx.task_id, "findings": payload,
                        "summary": state.get("summary") or "", "duration_ms": duration_ms}}


async def _load_finding(session, user_id: str, finding_id: str):
    """finding + 归属链（task→repo→user）一次取齐；不存在/无权抛 InvalidInputError。"""
    row = (await session.execute(text(
        "SELECT f.id, f.hitl_status, f.suggested_diff, f.title, f.file, "
        "f.start_line, f.end_line, r.path "
        "FROM review_findings f "
        "JOIN review_tasks t ON t.id = f.task_id "
        "JOIN repos r ON r.id = t.repo_id "
        "WHERE f.id = :fid AND t.user_id = :uid"),
        {"fid": uuid.UUID(finding_id), "uid": uuid.UUID(user_id)})).first()
    if row is None:
        raise InvalidInputError("finding 不存在或无权访问", agent_type="code_review")
    return row


async def apply_finding(user_id: str, finding_id: str) -> dict:
    """HitL 应用补丁：applier 预检+apply+commit，成功后落 applied + commit hash。
    冲突抛 ReviewApplyConflictError（endpoint 409，finding 保持 pending）。"""
    async with AsyncSessionLocal() as session:
        row = await _load_finding(session, user_id, finding_id)
        if row.hitl_status != "pending":
            raise ReviewHitlStateError(
                f"finding 已 {row.hitl_status}，不可重复 apply", agent_type="code_review")
        if not row.suggested_diff:
            raise InvalidInputError(
                "该 finding 无补丁（suggested_diff 为空），不能自动应用", agent_type="code_review")
        msg = (f"fix(code_review): {row.title} "
               f"({row.file} L{row.start_line}-L{row.end_line})")[:200]
        sha = await apply_finding_patch(row.path, row.suggested_diff, msg)
        res = await session.execute(text(
            "UPDATE review_findings SET hitl_status = 'applied', applied_commit = :sha "
            "WHERE id = :fid AND hitl_status = 'pending'"),
            {"sha": sha, "fid": uuid.UUID(finding_id)})
        if res.rowcount == 0:  # 并发 reject 抢先：commit 已落库，如实记录并报错待人工核对
            logger.error("orchestrator.apply_race", finding_id=finding_id, commit=sha)
            raise ReviewHitlStateError(
                "finding 状态已被并发变更，但补丁已提交", agent_type="code_review",
                details={"commit": sha})
        await session.commit()
    return {"finding_id": finding_id, "hitl_status": "applied", "applied_commit": sha}


async def reject_finding(user_id: str, finding_id: str) -> dict:
    """HitL 拒绝建议：仅 pending 可拒，条件更新防并发双写。"""
    async with AsyncSessionLocal() as session:
        row = await _load_finding(session, user_id, finding_id)
        if row.hitl_status != "pending":
            raise ReviewHitlStateError(
                f"finding 已 {row.hitl_status}，不可重复 reject", agent_type="code_review")
        res = await session.execute(text(
            "UPDATE review_findings SET hitl_status = 'rejected' "
            "WHERE id = :fid AND hitl_status = 'pending'"),
            {"fid": uuid.UUID(finding_id)})
        if res.rowcount == 0:
            raise ReviewHitlStateError(
                "finding 状态已被并发变更", agent_type="code_review")
        await session.commit()
    return {"finding_id": finding_id, "hitl_status": "rejected"}
