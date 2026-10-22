# backend/api/v1/repos.py
# 仓库管理接口：列表（只读）+ 登记 + 触发索引（各 Agent 共用，与 code_qa 解耦）。
# 索引是长耗时操作（扫描/切片/向量化），用后台任务异步执行，接口立即返回 indexing，
# 前端轮询 GET /repos 观察 index_status 变化（pending/indexing -> indexed/failed）。
import asyncio
import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.agents.code_qa.indexer import index_repo
from backend.core.logger import get_logger
from backend.core.knowledge_base import VectorStore
from backend.dependencies import get_current_user, get_db
from backend.services.code_fix import running_task_ids

router = APIRouter(prefix="/repos", tags=["repos"])
logger = get_logger(__name__)


class RegisterRepoRequest(BaseModel):
    """登记仓库请求体：name 可空（默认取路径末段）。"""
    path: str = Field(..., min_length=1, description="本地仓库绝对路径")
    name: str = Field("", max_length=128, description="展示名，空则取路径末段")


def _serialize(r) -> dict:
    return {"id": str(r.id), "name": r.name, "path": r.path,
            "index_status": r.index_status,
            "last_indexed_at": r.last_indexed_at.isoformat() if r.last_indexed_at else None,
            "created_at": r.created_at.isoformat()}


@router.get("")
async def list_repos(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """当前用户的仓库列表（含索引状态，index_status != 'indexed' 的仓库前端禁用问答）。"""
    rows = (await db.execute(text(
        "SELECT id, name, path, index_status, last_indexed_at, created_at "
        "FROM repos WHERE user_id = :uid ORDER BY created_at DESC"),
        {"uid": current_user["user_id"]})).all()
    return [_serialize(r) for r in rows]


@router.post("", status_code=201)
async def register_repo(
    req: RegisterRepoRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """登记一个本地仓库（index_status 初始 pending，需再调 /{id}/index 建索引）。"""
    path = os.path.abspath(req.path.strip())
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail=f"路径不存在或不是目录：{path}")
    name = req.name.strip() or os.path.basename(path.rstrip("\\/")) or path

    try:
        row = (await db.execute(text(
            "INSERT INTO repos (user_id, name, path) VALUES (:uid, :name, :path) "
            "RETURNING id, name, path, index_status, last_indexed_at, created_at"),
            {"uid": current_user["user_id"], "name": name, "path": path})).first()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="该路径已登记过")
    return _serialize(row)


async def _index_background(repo_id: str) -> None:
    """后台跑全量索引；index_repo 内部已做 CAS 抢锁与失败置 failed，这里只兜底记日志。"""
    try:
        summary = await index_repo(repo_id)
        logger.info("repos.index_done", repo_id=repo_id,
                    files=summary.files_indexed, chunks=summary.chunks_created)
    except Exception as e:  # noqa: BLE001 - 后台任务不能把异常抛回事件循环
        logger.error("repos.index_failed", repo_id=repo_id, error=str(e))


@router.post("/{repo_id}/index")
async def trigger_index(
    repo_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """触发全量索引（后台异步）。已在索引中返回 409 防并发重复触发。"""
    row = (await db.execute(text(
        "SELECT index_status FROM repos WHERE id = :rid AND user_id = :uid"),
        {"rid": repo_id, "uid": current_user["user_id"]})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="仓库不存在或无权访问")
    if row.index_status == "indexing":
        raise HTTPException(status_code=409, detail="该仓库正在索引中，请稍后再试")

    asyncio.create_task(_index_background(repo_id))
    return {"id": repo_id, "index_status": "indexing"}


@router.delete('/{repo_id}', status_code=204)
async def delete_repo(repo_id: UUID, current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    """删除登记、索引和级联审查记录；不删除磁盘目录及问答历史。"""
    params = {'rid': repo_id, 'uid': UUID(current_user['user_id'])}
    row = (await db.execute(text(
        'SELECT id,index_status FROM repos WHERE id=:rid AND user_id=:uid FOR UPDATE'), params)).first()
    if row is None:
        raise HTTPException(status_code=404, detail='仓库不存在或无权访问')
    if row.index_status == 'indexing':
        raise HTTPException(status_code=409, detail='仓库正在索引，请等待完成后再删除')
    # 与新建审查/问答的外键锁、修复入口的 finding 行锁协调。
    await db.execute(text('SELECT id FROM review_tasks WHERE repo_id=:rid FOR UPDATE'), params)
    await db.execute(text(
        'SELECT f.id FROM review_findings f JOIN review_tasks t ON t.id=f.task_id '
        'WHERE t.repo_id=:rid FOR UPDATE OF f'), params)
    params['jobs'] = [UUID(task_id) for task_id in running_task_ids()]
    active = (await db.execute(text(
        "SELECT 1 FROM review_tasks WHERE repo_id=:rid AND status='running' "
        "UNION ALL SELECT 1 FROM agent_runs WHERE repo_id=:rid AND status IN ('running','awaiting_confirm') "
        'UNION ALL SELECT 1 FROM fix_tasks x JOIN review_findings f ON f.id=x.finding_id '
        'JOIN review_tasks t ON t.id=f.task_id WHERE t.repo_id=:rid '
        "AND (x.status IN ('pending','preparing','analyzing','fixing','validating','ready') "
        'OR x.id=ANY(CAST(:jobs AS uuid[]))) LIMIT 1'), params)).first()
    if active:
        raise HTTPException(status_code=409, detail='仓库有运行中或待确认的任务，请先完成或取消后再删除')
    try:
        await asyncio.to_thread(VectorStore.delete_repo, str(repo_id))
    except Exception:
        logger.exception('repos.delete_vectors_failed', repo_id=str(repo_id))
        raise HTTPException(status_code=503, detail='向量索引清理失败，仓库记录已保留，请稍后重试')
    try:
        await db.execute(text('DELETE FROM repos WHERE id=:rid AND user_id=:uid'), params)
        await db.commit()
    except Exception:
        await db.rollback()
        # 跨库提交不能原子化：若删除记录失败，标记为待恢复，避免继续使用缺失向量。
        await db.execute(text("UPDATE repos SET index_status='failed' WHERE id=:rid AND user_id=:uid"), params)
        await db.commit()
        logger.exception('repos.delete_record_failed', repo_id=str(repo_id))
        raise HTTPException(status_code=503, detail='仓库记录删除失败，请重试删除或重新索引')
