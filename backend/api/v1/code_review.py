# backend/api/v1/code_review.py
# Code Review 接口层：SSE 流式审查 + 任务列表/详情 + HitL apply/reject。
# 本层只做 HTTP 语义（鉴权注入、参数校验、异常映射、SSE 格式化），业务编排在 services/orchestrator.py。
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.core.exceptions import (CodePilotBaseError, InvalidInputError,
                                     ReviewApplyConflictError, ReviewHitlStateError)
from backend.dependencies import get_current_user, get_db
from backend.services.orchestrator import (apply_finding, prepare_review,
                                           reject_finding, stream_review)

router = APIRouter(prefix="/code-review", tags=["code_review"])


class ReviewRequest(BaseModel):
    """审查请求体：mode/ref 组合合法性由 orchestrator 流前校验（400）。"""
    repo_id: UUID = Field(..., description="目标仓库 ID（须属于当前用户；不要求已索引）")
    mode: str = Field("uncommitted", description="uncommitted / range / commit")
    base_ref: str = Field("", max_length=200, description="range 模式起点")
    head_ref: str = Field("", max_length=200, description="range 终点 / commit 模式目标")
    review_basis: str = Field("", max_length=8000,
                              description="审查依据（业务规则文档+用户附注），空则无依据审查")


def _map_biz_error(e: CodePilotBaseError) -> HTTPException:
    """业务异常 -> HTTP 状态码：补丁冲突/HitL 状态非法 409，输入/权限 400。"""
    if isinstance(e, (ReviewApplyConflictError, ReviewHitlStateError)):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, InvalidInputError):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@router.post("/tasks")
async def create_review(
    req: ReviewRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """SSE 流式审查。事件序列：meta -> progress*/comment* -> done / error。
    校验失败在流开始前以普通 HTTP 错误返回；流开始后的失败走 error 事件。"""
    graph = request.app.state.code_review_graph
    try:
        ctx = await prepare_review(
            user_id=current_user["user_id"],
            repo_id=str(req.repo_id),
            mode=req.mode,
            base_ref=req.base_ref,
            head_ref=req.head_ref,
            review_basis=req.review_basis,
        )
    except CodePilotBaseError as e:
        raise _map_biz_error(e) from e

    async def sse():
        async for evt in stream_review(graph, ctx):
            payload = json.dumps(evt["data"], ensure_ascii=False)
            yield f"event: {evt['event']}\ndata: {payload}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        # 禁缓存 + 禁 Nginx 缓冲，保证 progress/comment 即时送达前端
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks")
async def list_tasks(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """当前用户的审查任务列表（最近 50 条，含状态与统计供历史/待确认队列渲染）。"""
    rows = (await db.execute(text(
        "SELECT id, repo_id, mode, base_ref, head_ref, status, summary, "
        "diff_stats, error_message, duration_ms, created_at "
        "FROM review_tasks WHERE user_id = :uid "
        "ORDER BY created_at DESC LIMIT 50"),
        {"uid": current_user["user_id"]})).all()
    return [{"id": str(r.id), "repo_id": str(r.repo_id), "mode": r.mode,
             "base_ref": r.base_ref, "head_ref": r.head_ref, "status": r.status,
             "summary": r.summary, "diff_stats": r.diff_stats,
             "error_message": r.error_message, "duration_ms": r.duration_ms,
             "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: UUID,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """任务详情 + 全量 findings（severity 严重度倒序），供页面刷新后回看与 HitL 操作。"""
    task = (await db.execute(text(
        "SELECT id, repo_id, mode, base_ref, head_ref, review_basis, status, summary, "
        "diff_stats, error_message, duration_ms, created_at "
        "FROM review_tasks WHERE id = :tid AND user_id = :uid"),
        {"tid": task_id, "uid": current_user["user_id"]})).first()
    if task is None:
        raise HTTPException(status_code=404, detail="审查任务不存在或无权访问")
    rows = (await db.execute(text(
        "SELECT id, file, start_line, end_line, severity, category, title, suggestion, "
        "suggested_diff, hitl_status, applied_commit, source, created_at "
        "FROM review_findings WHERE task_id = :tid "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
        "start_line"),
        {"tid": task_id})).all()
    return {
        "id": str(task.id), "repo_id": str(task.repo_id), "mode": task.mode,
        "base_ref": task.base_ref, "head_ref": task.head_ref,
        "review_basis": task.review_basis, "status": task.status,
        "summary": task.summary, "diff_stats": task.diff_stats,
        "error_message": task.error_message, "duration_ms": task.duration_ms,
        "created_at": task.created_at.isoformat(),
        "findings": [{"id": str(r.id), "file": r.file, "start_line": r.start_line,
                      "end_line": r.end_line, "severity": r.severity,
                      "category": r.category, "title": r.title,
                      "suggestion": r.suggestion, "suggested_diff": r.suggested_diff,
                      "hitl_status": r.hitl_status, "applied_commit": r.applied_commit,
                      "source": r.source, "created_at": r.created_at.isoformat()}
                     for r in rows],
    }


@router.post("/findings/{finding_id}/apply")
async def apply_finding_endpoint(
    finding_id: UUID,
    current_user: dict = Depends(get_current_user),
):
    """HitL 应用补丁：预检+apply+commit 成功返回 applied + commit hash。
    补丁与当前代码冲突 409（finding 保持 pending，待人工处理）。"""
    try:
        return await apply_finding(current_user["user_id"], str(finding_id))
    except CodePilotBaseError as e:
        raise _map_biz_error(e) from e


@router.post("/findings/{finding_id}/reject")
async def reject_finding_endpoint(
    finding_id: UUID,
    current_user: dict = Depends(get_current_user),
):
    """HitL 拒绝建议：仅 pending 可拒，重复操作 409。"""
    try:
        return await reject_finding(current_user["user_id"], str(finding_id))
    except CodePilotBaseError as e:
        raise _map_biz_error(e) from e
