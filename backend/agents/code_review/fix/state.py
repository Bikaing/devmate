"""修复图共享状态。仅保存可序列化数据，不保存进程或工作区句柄。

ready 表示可供用户审阅；是否验证通过由 verification_status 单独表达。
轮次从 1 开始，attempt_count 初始为 0；首次修复加最多两次重试，共 3 轮。
"""
from typing import Literal, TypedDict


FixStatus = Literal[
    # analyzing 仅兼容历史任务的取消与重启恢复，新修复不进入该阶段。
    "pending", "preparing", "analyzing", "fixing", "validating", "ready",
    "failed", "cancelled", "applied",
]
VerificationStatus = Literal[
    "not_run", "passed", "failed", "insufficient", "error",
]


class VerificationResult(TypedDict):
    """单项检查证据；argv 由受控执行器执行，不直接拼接为 shell 命令。

output 保存截断后的输出；error 表达超时、工具缺失等执行异常。
"""
    argv: list[str]
    exit_code: int | None
    status: VerificationStatus
    output: str
    duration_ms: int
    error: str | None


class FixState(TypedDict, total=False):
    task_id: str
    finding_id: str
    repo_id: str
    repo_path: str
    finding: dict  # 原审查 finding 的快照，避免修复中丢失问题依据
    review_basis: str

    # base_commit 不足以表示未提交修改；snapshot_id 标识实际输入代码快照。
    base_commit: str
    snapshot_id: str
    workspace_path: str | None
    status: FixStatus
    attempt_count: int
    max_attempts: int
    verification_profile: str

    contexts: list[dict]
    patch: str | None  # 相对输入快照的累计 unified diff
    changed_files: list[str]
    baseline_results: list[VerificationResult]
    verification_status: VerificationStatus
    verification_results: list[VerificationResult]
    verification_summary: str | None
    error: str | None
    applied_commit: str | None
