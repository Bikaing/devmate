# backend/agents/code_review/applier.py
# HitL 打补丁：git apply --check 预检（冲突抛 ReviewApplyConflictError，finding 保持 pending）
# -> 真实 apply -> 仅 git add 补丁涉及路径 -> 以 Agent 身份 commit -> 返回 commit hash。
# 只提交补丁涉及文件：工作区里用户自己的其他未提交改动不会被裹进 commit。
import asyncio
import os
import subprocess
import tempfile

from backend.core.exceptions import (AgentExecutionError, InvalidInputError,
                                     ReviewApplyConflictError)
from backend.core.logger import get_logger

logger = get_logger(__name__)

# Agent 提交身份：用户仓库未配 user.name/email 时 commit 会失败，这里显式注入兜底
_COMMIT_IDENTITY = ["-c", "user.name=DevMate", "-c", "user.email=devmate@local"]


def _patch_paths(patch: str) -> list[str]:
    """从 unified diff 头解析补丁涉及的文件路径（去重保序）；/dev/null 侧跳过。"""
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            p = line[6:].split("\t")[0].strip()  # 传统 diff 头可能带 \t 时间戳
            if p and p != "/dev/null" and p not in paths:
                paths.append(p)
    return paths


async def _git(repo_path: str, *args: str) -> subprocess.CompletedProcess:
    """git 子进程约定（镜像 diff_parser/indexer）：quotePath 关闭、超时 120s；返回 CompletedProcess 由调用方判定。"""
    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=repo_path,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    return await asyncio.to_thread(_run)


async def apply_finding_patch(repo_path: str, suggested_diff: str, commit_message: str) -> str:
    """预检 -> apply -> 精确 add -> commit，返回 commit hash；冲突抛 ReviewApplyConflictError。"""
    patch = (suggested_diff or "").strip()
    if not patch:
        raise InvalidInputError("suggested_diff 为空，无法应用补丁", agent_type="code_review")
    paths = _patch_paths(patch)
    if not paths:
        raise InvalidInputError("补丁中解析不到涉及文件路径", agent_type="code_review")

    # 补丁落临时文件：LF 行尾是 git apply 的硬要求，Windows 默认 CRLF 会全片冲突
    fd, patch_file = tempfile.mkstemp(suffix=".patch", prefix="devmate_review_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(patch if patch.endswith("\n") else patch + "\n")

        check = await _git(repo_path, "apply", "--check", patch_file)
        if check.returncode != 0:
            raise ReviewApplyConflictError(
                f"补丁与当前代码冲突: {check.stderr.strip()[:300]}",
                agent_type="code_review", details={"paths": paths})
        applied = await _git(repo_path, "apply", patch_file)
        if applied.returncode != 0:  # 预检后仍失败（竞态）：同冲突语义处理
            raise ReviewApplyConflictError(
                f"补丁应用失败: {applied.stderr.strip()[:300]}",
                agent_type="code_review", details={"paths": paths})
        add = await _git(repo_path, "add", "--", *paths)
        if add.returncode != 0:
            raise AgentExecutionError(
                f"git add 失败: {add.stderr.strip()[:300]}", agent_type="code_review")
        commit = await _git(repo_path, *_COMMIT_IDENTITY, "commit", "-m", commit_message)
        if commit.returncode != 0:
            raise AgentExecutionError(
                f"git commit 失败: {commit.stderr.strip()[:300]}", agent_type="code_review")
        head = await _git(repo_path, "rev-parse", "HEAD")
        if head.returncode != 0:
            raise AgentExecutionError(
                f"git rev-parse 失败: {head.stderr.strip()[:300]}", agent_type="code_review")
        sha = head.stdout.strip()
        logger.info("code_review.patch_applied", commit=sha, paths=paths)
        return sha
    finally:
        os.remove(patch_file)
