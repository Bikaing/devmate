"""代码修复任务接口：后台运行、轮询证据、取消与明确确认应用。"""
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Literal

from fastapi import HTTPException
from backend.core.exceptions import (CodePilotBaseError, InvalidInputError,
                                     ReviewApplyConflictError, ReviewHitlStateError)
from backend.dependencies import get_current_user
from backend.services import code_fix as service

router = APIRouter(prefix='/code-fix', tags=['code_fix'])


class FixRequest(BaseModel):
    verification_profile: Literal['pytest', 'unittest', 'npm-build'] = 'pytest'


class ApplyRequest(BaseModel):
    confirmed: Literal[True]


async def _call(function, *args):
    try:
        return await function(*args)
    except CodePilotBaseError as exc:
        status = (409 if isinstance(exc, (ReviewApplyConflictError, ReviewHitlStateError))
                  else 400 if isinstance(exc, InvalidInputError) else 500)
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post('/findings/{finding_id}/tasks', status_code=202)
async def start(finding_id: UUID, body: FixRequest, user=Depends(get_current_user)):
    return await _call(service.start, user['user_id'], str(finding_id), body.verification_profile)


@router.get('/findings/{finding_id}/tasks')
async def list_tasks(finding_id: UUID, user=Depends(get_current_user)):
    return await _call(service.list_for_finding, user['user_id'], str(finding_id))


@router.get('/tasks/{task_id}')
async def detail(task_id: UUID, user=Depends(get_current_user)):
    return await _call(service.detail, user['user_id'], str(task_id))


@router.post('/tasks/{task_id}/cancel')
async def cancel(task_id: UUID, user=Depends(get_current_user)):
    return await _call(service.cancel, user['user_id'], str(task_id))


@router.post('/tasks/{task_id}/apply')
async def apply(task_id: UUID, body: ApplyRequest, user=Depends(get_current_user)):
    return await _call(service.apply, user['user_id'], str(task_id))
