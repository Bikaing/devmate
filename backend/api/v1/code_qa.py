# backend/api/v1/code_qa.py
# Code QA 接口层：SSE 流式问答 + 会话列表 + 历史消息回放。
# 本层只做 HTTP 语义（鉴权注入、参数校验、异常映射、SSE 格式化），业务编排在 services/orchestrator.py。
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.core.exceptions import CodePilotBaseError, InvalidInputError, RepoNotIndexedError
from backend.dependencies import get_current_user, get_db
from backend.services.orchestrator import prepare_code_qa, stream_code_qa

router = APIRouter(prefix="/code-qa", tags=["code_qa"])


class ChatRequest(BaseModel):
    """问答请求体：conversation_id 为空则新建会话。"""
    repo_id: UUID = Field(..., description="目标仓库 ID（须属于当前用户且已索引）")
    query: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    conversation_id: UUID | None = Field(None, description="会话 ID，空则新建")
    web_search: bool = Field(False, description="联网开关：ON 时并行 web 搜索补充通用/时效信息")


def _map_biz_error(e: CodePilotBaseError) -> HTTPException:
    """业务异常 -> HTTP 状态码：索引未就绪 409（可稍后重试），输入/权限 400。"""
    if isinstance(e, RepoNotIndexedError):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, InvalidInputError):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@router.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """SSE 流式问答。事件序列：meta -> token* -> done / error。
    校验失败在流开始前以普通 HTTP 错误返回；流开始后的失败走 error 事件。"""
    graph = request.app.state.code_qa_graph
    try:
        ctx = await prepare_code_qa(
            user_id=current_user["user_id"],
            repo_id=str(req.repo_id),
            conversation_id=str(req.conversation_id) if req.conversation_id else None,
            query=req.query,
            web_search=req.web_search,
        )
    except CodePilotBaseError as e:
        raise _map_biz_error(e) from e

    async def sse():
        async for evt in stream_code_qa(graph, ctx, req.query):
            payload = json.dumps(evt["data"], ensure_ascii=False)
            yield f"event: {evt['event']}\ndata: {payload}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        # 禁缓存 + 禁 Nginx 缓冲，保证 token 即时送达前端
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations")
async def list_conversations(
    status: str = "active",
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """会话列表（默认只列 active，按最近活跃倒序）。"""
    rows = (await db.execute(text(
        "SELECT id, title, agent_type, status, last_active_at, created_at "
        "FROM conversations WHERE user_id = :uid AND status = :st "
        "ORDER BY last_active_at DESC LIMIT 100"),
        {"uid": current_user["user_id"], "st": status})).all()
    return [{"id": str(r.id), "title": r.title, "agent_type": r.agent_type,
             "status": r.status, "last_active_at": r.last_active_at.isoformat(),
             "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: UUID,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """历史消息回放（含 msg_type 与 citations，前端按契约渲染卡片）。"""
    owned = (await db.execute(text(
        "SELECT 1 FROM conversations WHERE id = :cid AND user_id = :uid"),
        {"cid": conversation_id, "uid": current_user["user_id"]})).first()
    if owned is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    rows = (await db.execute(text(
        "SELECT id, role, msg_type, content, citations, web_sources, created_at "
        "FROM messages WHERE conversation_id = :cid ORDER BY created_at"),
        {"cid": conversation_id})).all()
    return [{"id": str(r.id), "role": r.role, "msg_type": r.msg_type,
             "content": r.content, "citations": r.citations,
             "web_sources": r.web_sources,
             "created_at": r.created_at.isoformat()} for r in rows]
