# backend/api/v1/doc_insight.py
# Doc Insight 接口层：SSE 流式洞察 + 任务列表/详情。
# 本层只做 HTTP 语义（鉴权注入、参数校验、异常映射、SSE 格式化），业务编排在 services/orchestrator.py。
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.core.exceptions import CodePilotBaseError, InvalidInputError
from backend.dependencies import get_current_user, get_db
from backend.services.orchestrator import prepare_doc_insight, stream_doc_insight

router = APIRouter(prefix="/doc-insight", tags=["doc_insight"])


class DocInsightRequest(BaseModel):
    """洞察请求体：MVP 直接收文本（前端读 md/txt 后传），multipart 文件上传二期再做。"""
    title: str = Field(..., min_length=1, max_length=200, description="文档标题")
    content: str = Field(..., min_length=1, description="文档原文（md/txt 纯文本）")


def _map_biz_error(e: CodePilotBaseError) -> HTTPException:
    """业务异常 -> HTTP 状态码：输入/权限 400，其余 500。"""
    if isinstance(e, InvalidInputError):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@router.post("/tasks")
async def create_doc_task(
    req: DocInsightRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """SSE 流式洞察。事件序列：meta -> progress* -> token* -> done / error。
    校验失败在流开始前以普通 HTTP 错误返回；流开始后的失败走 error 事件。"""
    graph = request.app.state.doc_insight_graph
    try:
        ctx = await prepare_doc_insight(
            user_id=current_user["user_id"],
            title=req.title,
            content=req.content,
        )
    except CodePilotBaseError as e:
        raise _map_biz_error(e) from e

    async def sse():
        async for evt in stream_doc_insight(graph, ctx):
            payload = json.dumps(evt["data"], ensure_ascii=False)
            yield f"event: {evt['event']}\ndata: {payload}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        # 禁缓存 + 禁 Nginx 缓冲，保证 progress/token 即时送达前端
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks")
async def list_doc_tasks(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """当前用户的洞察任务列表（最近 50 条，不含 content/report 大字段供列表渲染）。"""
    rows = (await db.execute(text(
        "SELECT id, title, status, error_message, duration_ms, created_at "
        "FROM doc_tasks WHERE user_id = :uid "
        "ORDER BY created_at DESC LIMIT 50"),
        {"uid": current_user["user_id"]})).all()
    return [{"id": str(r.id), "title": r.title, "status": r.status,
             "error_message": r.error_message, "duration_ms": r.duration_ms,
             "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/tasks/{task_id}")
async def get_doc_task(
    task_id: UUID,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """任务详情 + 完整报告与原文（历史回看：点开旧任务重新渲染报告）。"""
    task = (await db.execute(text(
        "SELECT id, title, content, status, report, error_message, duration_ms, created_at "
        "FROM doc_tasks WHERE id = :tid AND user_id = :uid"),
        {"tid": task_id, "uid": current_user["user_id"]})).first()
    if task is None:
        raise HTTPException(status_code=404, detail="洞察任务不存在或无权访问")
    return {"id": str(task.id), "title": task.title, "content": task.content,
            "status": task.status, "report": task.report,
            "error_message": task.error_message, "duration_ms": task.duration_ms,
            "created_at": task.created_at.isoformat()}
