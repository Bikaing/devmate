"""隔离修复工作区。Git 对象会写入共享对象库，原工作树、索引和分支不变。

uncommitted 快照包含 tracked 当前内容与未忽略的 untracked 文件；忽略文件
（例如 .env、虚拟环境）不会复制。调用方必须在审查输入仍有效时创建快照。
worktree 是文件隔离，不是执行不可信命令的安全沙箱。
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from uuid import UUID

from backend.core.exceptions import AgentExecutionError, InvalidInputError


@dataclass(frozen=True)
class Workspace:
    task_id: str
    repo_path: str
    workspace_path: str
    base_commit: str
    snapshot_id: str


@dataclass(frozen=True)
class WorkspacePatch:
    patch: str
    changed_files: list[str]


def _git(repo: Path, *args: str, env: dict | None = None,
         data: bytes | None = None) -> bytes:
    process_env = os.environ.copy()
    # 避免继承外部 Git 定位变量，将命令意外指向另一仓库或索引。
    for key in list(process_env):
        if key.startswith('GIT_'):
            del process_env[key]
    process_env.update(env or {})
    try:
        result = subprocess.run(
            ['git', '-c', 'core.quotePath=false', *args], cwd=repo,
            env=process_env, input=data, capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentExecutionError('Git workspace operation failed',
                                  agent_type='code_fix') from exc
    if result.returncode:
        raise AgentExecutionError(
            result.stderr.decode('utf-8', errors='replace')[:1000],
            agent_type='code_fix', details={'operation': args[0]},
        )
    return result.stdout


def _tree(repo: Path) -> str:
    """用独立索引捕获磁盘内容，不改动真实暂存区。"""
    with tempfile.TemporaryDirectory(prefix='devmate_index_') as temp:
        env = {'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        _git(repo, 'read-tree', 'HEAD', env=env)
        _git(repo, 'add', '--all', '--', '.', env=env)
        return _git(repo, 'write-tree', env=env).decode().strip()


def _create(repo_path: str, task_id: str, mode: str, head_ref: str,
            workspace_root: str | None) -> Workspace:
    try:
        task_id = str(UUID(task_id))
    except ValueError as exc:
        raise InvalidInputError('task_id must be a UUID', agent_type='code_fix') from exc
    if mode not in ('uncommitted', 'commit', 'range'):
        raise InvalidInputError('Unsupported review mode', agent_type='code_fix')
    if mode != 'uncommitted' and not head_ref.strip():
        raise InvalidInputError('head_ref is required', agent_type='code_fix')
    repo = Path(repo_path).resolve(strict=True)
    top = Path(os.fsdecode(_git(repo, 'rev-parse', '--show-toplevel')).strip()).resolve()
    if top != repo:
        raise InvalidInputError('repo_path must be the repository root', agent_type='code_fix')
    # 子模块内容不由父仓库 tree 表达，第一版显式拒绝，避免产生不完整快照。
    if _git(repo, 'ls-files', '--stage').find(b'160000 ') >= 0:
        raise InvalidInputError('Submodules are not supported', agent_type='code_fix')
    ref = 'HEAD' if mode == 'uncommitted' else head_ref
    base = _git(repo, 'rev-parse', '--verify', '--end-of-options',
                ref + '^{commit}').decode().strip()
    root = (Path(workspace_root) if workspace_root else
            Path(tempfile.gettempdir()) / 'devmate_code_fix').resolve()
    if root == repo or repo in root.parents:
        raise InvalidInputError('Workspace root must be outside the source repository',
                                agent_type='code_fix')
    root.mkdir(parents=True, exist_ok=True)
    container = root / task_id
    container.mkdir()  # 同任务不能覆盖已有工作区
    worktree = container / 'worktree'
    added = False
    try:
        snapshot = base
        if mode == 'uncommitted':
            tree = _tree(repo)
            identity = {'GIT_AUTHOR_NAME': 'DevMate', 'GIT_AUTHOR_EMAIL': 'devmate@local',
                        'GIT_COMMITTER_NAME': 'DevMate', 'GIT_COMMITTER_EMAIL': 'devmate@local'}
            snapshot = _git(repo, 'commit-tree', tree, '-p', base, env=identity,
                            data=b'DevMate repair input snapshot\n').decode().strip()
        _git(repo, 'worktree', 'add', '--detach', str(worktree), snapshot)
        added = True
        # 即使工作区清理，待确认补丁的输入仍需避免被 git gc 回收。
        _git(repo, 'update-ref', f'refs/devmate/fix/{task_id}', snapshot)
        workspace = Workspace(task_id, str(repo), str(worktree), base, snapshot)
        (container / 'workspace.json').write_text(
            json.dumps(asdict(workspace), ensure_ascii=False), encoding='utf-8')
        return workspace
    except BaseException:
        if added:
            # 清理失败时保留目录和 Git 注册信息，供人工恢复。
            _git(repo, 'worktree', 'remove', '--force', str(worktree))
        if not any(container.iterdir()):
            container.rmdir()
        raise


def _validate(workspace: Workspace) -> Path:
    path = Path(workspace.workspace_path)
    if path.is_symlink() or path.parent.is_symlink():
        raise InvalidInputError('Symlink workspace is not allowed', agent_type='code_fix')
    path = path.resolve()
    if path.name != 'worktree' or path.parent.name != str(UUID(workspace.task_id)):
        raise InvalidInputError('Invalid managed workspace path', agent_type='code_fix')
    marker = path.parent / 'workspace.json'
    if json.loads(marker.read_text(encoding='utf-8')) != asdict(workspace):
        raise InvalidInputError('Workspace metadata mismatch', agent_type='code_fix')
    repo = Path(workspace.repo_path).resolve()
    if path == repo or repo in path.parents or path in repo.parents:
        raise InvalidInputError('Unsafe workspace path', agent_type='code_fix')
    for candidate in (repo, path):
        common = Path(os.fsdecode(_git(candidate, 'rev-parse', '--path-format=absolute',
                                       '--git-common-dir')).strip()).resolve()
        if candidate == repo:
            source_common = common
        elif common != source_common:
            raise InvalidInputError('Workspace repository mismatch', agent_type='code_fix')
    return path


def _extract(workspace: Workspace) -> WorkspacePatch:
    path = _validate(workspace)
    tree = _tree(path)
    flags = ['--no-ext-diff', '--no-textconv', '--no-renames']
    patch = _git(path, 'diff', *flags, '--binary', '--full-index',
                 workspace.snapshot_id, tree, '--').decode('utf-8')
    names = _git(path, 'diff', *flags, '--name-only', '-z',
                 workspace.snapshot_id, tree, '--')
    return WorkspacePatch(patch, [os.fsdecode(p) for p in names.split(b'\0') if p])


def _cleanup(workspace: Workspace) -> None:
    path = _validate(workspace)
    # 只让 Git 删除已验证的单个工作区，不递归删除任意调用方路径。
    _git(Path(workspace.repo_path), 'worktree', 'remove', '--force', str(path))
    (path.parent / 'workspace.json').unlink()
    path.parent.rmdir()


async def create_workspace(repo_path: str, task_id: str, mode: str = 'uncommitted',
                           head_ref: str = '', workspace_root: str | None = None) -> Workspace:
    """建立任务独占的 detached worktree；commit/range 均使用 head_ref 版本。

    支持已有至少一个 commit 的普通仓库。不会 stash、checkout 或提交原分支。
    """
    return await asyncio.to_thread(_create, repo_path, task_id, mode, head_ref, workspace_root)


async def extract_patch(workspace: Workspace) -> WorkspacePatch:
    """返回相对输入快照的累计补丁，包含新增、删除和二进制文件。"""
    return await asyncio.to_thread(_extract, workspace)


async def cleanup_workspace(workspace: Workspace) -> None:
    """删除已登记工作区（含修复产生的文件）；失败保留记录并抛错。"""
    await asyncio.to_thread(_cleanup, workspace)


def capture_source(repo_path: str, mode: str, head_ref: str = '') -> tuple[str, str]:
    """返回审查输入的不可变 commit 与 tree，供修复前检查输入是否过期。"""
    repo = Path(repo_path).resolve()
    ref = 'HEAD' if mode == 'uncommitted' else head_ref
    commit = _git(repo, 'rev-parse', '--verify', '--end-of-options',
                  ref + '^{commit}').decode().strip()
    tree = (_tree(repo) if mode == 'uncommitted' else
            _git(repo, 'rev-parse', commit + '^{tree}').decode().strip())
    return commit, tree


def apply_workspace_patch(workspace: Workspace, patch: str) -> None:
    """将已审阅补丁应用到原工作树；严格比较快照，不改索引、不自动提交。

    调用方负责仓库级互斥与任务/用户鉴权；应用后不要再次执行本函数。
    """
    _validate(workspace)
    apply_saved_patch(workspace.repo_path, workspace.base_commit, workspace.snapshot_id, patch)


def apply_saved_patch(repo_path: str, base_commit: str, snapshot_id: str, patch: str) -> None:
    """应用服务端保存的补丁；参数来自已鉴权的持久化任务，不能直接接受客户端提供。"""
    repo = Path(repo_path).resolve()
    current = _git(repo, 'rev-parse', 'HEAD').decode().strip()
    expected = _git(repo, 'rev-parse', snapshot_id + '^{tree}').decode().strip()
    if current != base_commit or _tree(repo) != expected:
        from backend.core.exceptions import ReviewApplyConflictError
        raise ReviewApplyConflictError('原仓库已偏离修复输入，请重新审查并修复', agent_type='code_fix')
    if not patch.strip():
        raise InvalidInputError('修复补丁为空', agent_type='code_fix')
    data = patch.encode('utf-8')
    _git(repo, 'apply', '--check', '--binary', '-', data=data)
    _git(repo, 'apply', '--binary', '-', data=data)
