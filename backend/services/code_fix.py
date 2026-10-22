"""持久化修复任务。后台执行与 HTTP 生命周期分离，前端轮询读取证据。"""
import asyncio
import json
import threading
import time
import tempfile
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.agents.code_review.fix.runner import run_repair
from backend.agents.code_review.fix.workspace import (
    Workspace, cleanup_workspace, create_workspace,
    _git,
)
from pathlib import Path
from backend.core.exceptions import InvalidInputError, ReviewHitlStateError
from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal

logger = get_logger(__name__)
_jobs: dict[str, tuple[asyncio.Task, threading.Event]] = {}
ACTIVE = ('pending', 'preparing', 'analyzing', 'fixing', 'validating', 'ready')


def running_task_ids() -> list[str]:
    """包含终态之后仍在清理工作区的任务，供仓库删除保护使用。"""
    return [task_id for task_id, (job, _) in _jobs.items() if not job.done()]


async def _owned(session, user_id: str, task_id: str, lock=False):
    row = (await session.execute(text(
        'SELECT x.*, r.path AS repo_path, r.id AS repo_id FROM fix_tasks x '
        'JOIN review_findings f ON f.id=x.finding_id '
        'JOIN review_tasks t ON t.id=f.task_id JOIN repos r ON r.id=t.repo_id '
        'WHERE x.id=:id AND t.user_id=:uid' + (' FOR UPDATE OF x, f' if lock else '')),
        {'id': UUID(task_id), 'uid': UUID(user_id)})).mappings().first()
    if row is None:
        raise InvalidInputError('修复任务不存在或无权访问', agent_type='code_fix')
    return dict(row)


def _release_snapshot(repo_path: str, task_id: str):
    _git(Path(repo_path), 'update-ref', '-d', f'refs/devmate/fix/{UUID(task_id)}')


def _public(row):
    # 不暴露本机绝对路径；UUID/时间由 FastAPI 编码。
    return {k: v for k, v in row.items() if k not in ('repo_path', 'workspace_path')}


async def detail(user_id: str, task_id: str):
    async with AsyncSessionLocal() as session:
        row = await _owned(session, user_id, task_id)
        attempts = (await session.execute(text(
            'SELECT * FROM fix_attempts WHERE task_id=:id ORDER BY attempt_number'),
            {'id': UUID(task_id)})).mappings().all()
        return {**_public(row), 'attempts': [dict(a) for a in attempts]}


async def list_for_finding(user_id: str, finding_id: str):
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            'SELECT x.* FROM fix_tasks x JOIN review_findings f ON f.id=x.finding_id '
            'JOIN review_tasks t ON t.id=f.task_id '
            'WHERE x.finding_id=:fid AND t.user_id=:uid ORDER BY x.created_at DESC LIMIT 20'),
            {'fid': UUID(finding_id), 'uid': UUID(user_id)})).mappings().all()
        return [_public(dict(r)) for r in rows]


async def start(user_id: str, finding_id: str, profile: str):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            'SELECT f.*, t.mode, t.head_ref, t.input_commit, t.input_tree, t.review_basis, '
            'r.path FROM review_findings f JOIN review_tasks t ON t.id=f.task_id '
            'JOIN repos r ON r.id=t.repo_id WHERE f.id=:fid AND t.user_id=:uid FOR UPDATE OF f'),
            {'fid': UUID(finding_id), 'uid': UUID(user_id)})).mappings().first()
        if row is None:
            raise InvalidInputError('问题不存在或无权访问', agent_type='code_fix')
        row = dict(row)
        if row['hitl_status'] != 'pending':
            raise ReviewHitlStateError('问题已被处置', agent_type='code_fix')
        if not row['input_tree'] or not row['input_commit']:
            raise ReviewHitlStateError('此历史审查没有代码快照，请重新审查后再修复', agent_type='code_fix')
        finding = {k: row[k] for k in ('file', 'start_line', 'end_line', 'title', 'suggestion',
                                     'severity', 'category')}
        try:
            task_id = str((await session.execute(text(
                'INSERT INTO fix_tasks (finding_id, finding_snapshot, review_basis, '
                'verification_profile, max_attempts) VALUES (:fid, CAST(:finding AS jsonb), '
                ':basis, :profile, 3) RETURNING id'),
                {'fid': UUID(finding_id), 'finding': json.dumps(finding, ensure_ascii=False),
                 'basis': row['review_basis'], 'profile': profile})).scalar_one())
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ReviewHitlStateError('该问题已有运行中或待确认的修复任务', agent_type='code_fix') from exc
    stop = threading.Event()
    job = asyncio.create_task(_run(task_id, row, finding, profile, stop))
    _jobs[task_id] = (job, stop)
    job.add_done_callback(lambda _: _jobs.pop(task_id, None))
    return {'id': task_id, 'status': 'pending'}


async def _update(task_id: str, **fields):
    json_fields = {'baseline_results', 'verification_results', 'changed_files'}
    allowed = {'status', 'attempt_count', 'base_commit', 'snapshot_id', 'workspace_path',
               'patch', 'verification_status', 'error_message', 'duration_ms',
               'verification_summary', 'cleanup_error', *json_fields}
    assert fields.keys() <= allowed
    clauses, params = [], {'id': UUID(task_id)}
    for key, value in fields.items():
        if key in json_fields:
            value = json.dumps(value, ensure_ascii=False)
            clauses.append(f'{key}=CAST(:{key} AS jsonb)')
        else:
            clauses.append(f'{key}=:{key}')
        params[key] = value
    async with AsyncSessionLocal() as session:
        status = (await session.execute(text('SELECT status FROM fix_tasks WHERE id=:id FOR UPDATE'),
                                        {'id': UUID(task_id)})).scalar_one()
        if status == 'cancelled':
            raise asyncio.CancelledError()
        await session.execute(text('UPDATE fix_tasks SET ' + ', '.join(clauses) + ' WHERE id=:id'), params)
        if fields.get('status') in ('ready', 'failed'):
            await session.execute(text('UPDATE fix_tasks SET finished_at=NOW() WHERE id=:id'), params)
        await session.commit()


async def _run(task_id, source, finding, profile, stop):
    workspace = None
    started = time.monotonic()
    try:
        await _update(task_id, status='preparing')
        workspace = await create_workspace(source['path'], task_id, source['mode'],
                                           source['input_commit'])
        tree = await asyncio.to_thread(_git, Path(workspace.workspace_path),
                                       'rev-parse', workspace.snapshot_id + '^{tree}')
        if tree.decode().strip() != source['input_tree'] or workspace.base_commit != source['input_commit']:
            raise ReviewHitlStateError('代码已偏离审查输入，请重新审查', agent_type='code_fix')
        await _update(task_id, base_commit=workspace.base_commit, snapshot_id=workspace.snapshot_id,
                      workspace_path=workspace.workspace_path)

        async def emit(event, data):
            if stop.is_set():
                raise asyncio.CancelledError()
            if event == 'baseline':
                return
            if event == 'baseline_done':
                await _update(task_id, **data)
                return
            if event in ('fixing', 'validating'):
                await _update(task_id, status=event, attempt_count=data['attempt'],
                              **{k: v for k, v in data.items() if k in ('patch', 'changed_files')})
                async with AsyncSessionLocal() as session:
                    if event == 'fixing':
                        await session.execute(text(
                            'INSERT INTO fix_attempts (task_id, attempt_number, model_calls) VALUES (:id, :n, 1)'),
                            {'id': UUID(task_id), 'n': data['attempt']})
                    else:
                        await session.execute(text(
                            "UPDATE fix_attempts SET status='validating' WHERE task_id=:id AND attempt_number=:n"),
                            {'id': UUID(task_id), 'n': data['attempt']})
                    await session.commit()
            elif event == 'attempt_done':
                fields = {k: data[k] for k in ('patch', 'changed_files', 'verification_status',
                                              'verification_results', 'error_message')}
                await _update(task_id, **fields)
                async with AsyncSessionLocal() as session:
                    await session.execute(text(
                        'UPDATE fix_attempts SET status=:status, patch=:patch, '
                        'changed_files=CAST(:files AS jsonb), verification_status=:vs, '
                        'verification_results=CAST(:results AS jsonb), error_message=:err, '
                        'duration_ms=:dur, finished_at=NOW() WHERE task_id=:id AND attempt_number=:n'),
                        {'id': UUID(task_id), 'n': data['attempt'],
                         'status': 'failed' if data['error_message'] else 'completed',
                         'patch': data['patch'], 'files': json.dumps(data['changed_files']),
                         'vs': data['verification_status'],
                         'results': json.dumps(data['verification_results'], ensure_ascii=False),
                         'err': data['error_message'], 'dur': data['duration_ms']})
                    await session.commit()
            elif event in ('ready', 'failed'):
                await _update(task_id, status=event,
                              verification_status=data['verification_status'],
                              error_message=data.get('error_message'),
                              duration_ms=int((time.monotonic() - started) * 1000))

        await run_repair(workspace, finding, source['review_basis'] or '', profile, emit, stop.is_set)
    except asyncio.CancelledError:
        async with AsyncSessionLocal() as session:
            await session.execute(text(
                "UPDATE fix_tasks SET status='cancelled', finished_at=NOW() WHERE id=:id AND status<>'applied'"),
                {'id': UUID(task_id)})
            await session.execute(text(
                "UPDATE fix_attempts SET status='cancelled', finished_at=NOW() "
                "WHERE task_id=:id AND status IN ('analyzing','fixing','validating')"), {'id': UUID(task_id)})
            await session.commit()
    except Exception as exc:
        logger.error('code_fix.failed', task_id=task_id, error=str(exc))
        try:
            await _update(task_id, status='failed', error_message=str(exc)[:2000],
                          duration_ms=int((time.monotonic() - started) * 1000))
            async with AsyncSessionLocal() as session:
                await session.execute(text(
                    "UPDATE fix_attempts SET status='failed', error_message=:err, finished_at=NOW() "
                    "WHERE task_id=:id AND status IN ('analyzing','fixing','validating')"),
                    {'id': UUID(task_id), 'err': str(exc)[:2000]})
                await session.commit()
        except asyncio.CancelledError:
            pass
    finally:
        # patch 与证据已持久化，确认应用无需保留工作目录；失败也清理。
        if workspace:
            try:
                await cleanup_workspace(workspace)
            except Exception as exc:
                logger.error('code_fix.cleanup_failed', task_id=task_id, error=str(exc))
                async with AsyncSessionLocal() as session:
                    await session.execute(text('UPDATE fix_tasks SET cleanup_error=:err WHERE id=:id'),
                                          {'id': UUID(task_id), 'err': str(exc)[:1000]})
                    await session.commit()
            async with AsyncSessionLocal() as session:
                status = (await session.execute(text('SELECT status FROM fix_tasks WHERE id=:id'),
                                                {'id': UUID(task_id)})).scalar_one()
            if status != 'ready':
                try:
                    await asyncio.to_thread(_release_snapshot, workspace.repo_path, task_id)
                except Exception as exc:
                    logger.error('code_fix.snapshot_cleanup_failed', task_id=task_id, error=str(exc))


async def cancel(user_id: str, task_id: str):
    async with AsyncSessionLocal() as session:
        row = await _owned(session, user_id, task_id, lock=True)
        if row['status'] not in ACTIVE:
            raise ReviewHitlStateError('任务已结束，无法取消', agent_type='code_fix')
        await session.execute(text("UPDATE fix_tasks SET status='cancelled', finished_at=NOW() WHERE id=:id"),
                              {'id': UUID(task_id)})
        await session.commit()
    if task_id in _jobs:
        _jobs[task_id][1].set()
    elif row['snapshot_id']:
        try:
            await asyncio.to_thread(_release_snapshot, row['repo_path'], task_id)
        except Exception as exc:
            logger.error('code_fix.snapshot_cleanup_failed', task_id=task_id, error=str(exc))
    return {'id': task_id, 'status': 'cancelled'}


async def apply(user_id: str, task_id: str):
    async with AsyncSessionLocal() as session:
        row = await _owned(session, user_id, task_id, lock=True)
        if row['status'] != 'ready' or row['verification_status'] != 'passed':
            raise ReviewHitlStateError('仅通过验证且待确认的修复可以应用', agent_type='code_fix')
        pending = (await session.execute(text('SELECT hitl_status FROM review_findings WHERE id=:id'),
                                        {'id': row['finding_id']})).scalar_one()
        if pending != 'pending':
            raise ReviewHitlStateError('该问题已被处置', agent_type='code_fix')
        await session.execute(text('SELECT pg_advisory_xact_lock(hashtext(:repo))'),
                              {'repo': str(row['repo_id'])})
        # 工作区已清理，原仓库中的 snapshot 对象用于严格比较。无需读取工作区 marker。
        from backend.agents.code_review.fix.workspace import apply_saved_patch
        await asyncio.to_thread(apply_saved_patch, row['repo_path'], row['base_commit'],
                                row['snapshot_id'], row['patch'])
        await session.execute(text("UPDATE fix_tasks SET status='applied' WHERE id=:id"), {'id': UUID(task_id)})
        await session.execute(text(
            "UPDATE review_findings SET hitl_status='applied', applied_commit=NULL WHERE id=:id"),
            {'id': row['finding_id']})
        await session.commit()
    try:
        await asyncio.to_thread(_release_snapshot, row['repo_path'], task_id)
    except Exception as exc:
        logger.error('code_fix.snapshot_cleanup_failed', task_id=task_id, error=str(exc))
    return {'id': task_id, 'status': 'applied', 'applied_commit': None}


async def recover_interrupted():
    """单进程部署启动时终结上次未完成任务，避免永久占用活跃槽位。"""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            "UPDATE fix_tasks SET status='failed', error_message='服务重启中断，请重新发起修复', "
            "finished_at=NOW() WHERE status IN ('pending','preparing','analyzing','fixing','validating') RETURNING id"
        ))).all()
        for row in rows:
            await session.execute(text(
                "UPDATE fix_attempts SET status='failed', error_message='服务重启中断', finished_at=NOW() "
                "WHERE task_id=:id AND status IN ('analyzing','fixing','validating')"), {'id': row.id})
        await session.commit()
    # 清理上次进程中断后遗留的 worktree。只接受模块签名文件并再次验证归属。
    async with AsyncSessionLocal() as session:
        leftovers = (await session.execute(text(
            'SELECT x.id, r.path AS repo_path, x.status FROM fix_tasks x '
            'JOIN review_findings f ON f.id=x.finding_id '
            'JOIN review_tasks t ON t.id=f.task_id JOIN repos r ON r.id=t.repo_id '
            "WHERE x.status IN ('failed','cancelled','ready','applied')"
        ))).mappings().all()
    for row in leftovers:
        marker = Path(tempfile.gettempdir()) / 'devmate_code_fix' / str(row['id']) / 'workspace.json'
        try:
            if marker.is_file():
                workspace = Workspace(**json.loads(marker.read_text(encoding='utf-8')))
                if workspace.task_id != str(row['id']) or workspace.repo_path != row['repo_path']:
                    raise ValueError('workspace recovery metadata mismatch')
                await cleanup_workspace(workspace)
            if row['status'] != 'ready':
                await asyncio.to_thread(_release_snapshot, row['repo_path'], str(row['id']))
        except Exception as exc:
            logger.error('code_fix.recovery_cleanup_failed', task_id=str(row['id']), error=str(exc))


async def shutdown():
    jobs = list(_jobs.values())
    for _, stop in jobs:
        stop.set()
    if jobs:
        await asyncio.gather(*(job for job, _ in jobs), return_exceptions=True)
