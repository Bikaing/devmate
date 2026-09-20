# backend/agents/code_review/diff_parser.py
# diff 获取与解析：三模式 git diff -> 逐文件变更结构（diff_files）+ 汇总统计（diff_stats）。
# 约定：
#   - 行号锚定 head 版本（新文件侧）；删除文件 head 已不存在，hunk 行号退用旧文件侧；
#   - commit 模式用 git show（根提交安全，c^..c 对根提交报错）；--no-renames 让重命名退化为删+增简化解析；
#   - uncommitted 模式额外为 untracked 新文件合成「新增文件」变更（git diff 不含 untracked）；
#   - 二进制 / lockfile / 超宽变更进 diff_stats.skipped，不喂 LLM。
import asyncio
import os
import re
import subprocess

from backend.core.exceptions import InvalidInputError
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 解析与过滤常量
DIFF_FLAGS = ["--unified=3", "--no-color", "--no-renames", "--no-ext-diff"]
MAX_FILE_CHANGES = 2000           # 单文件 added+deleted 上限，超了按超宽跳过
MAX_UNTRACKED_BYTES = 512 * 1024  # untracked 新文件单文件上限，与 indexer 扫描口径一致
_LOCK_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
               "cargo.lock", "go.sum", "composer.lock", "gemfile.lock"}
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _is_lock(path: str) -> bool:
    name = os.path.basename(path).lower()
    return name in _LOCK_NAMES or name.endswith(".lock")


def _run_git(repo_path: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=repo_path,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


async def _git(repo_path: str, *args: str) -> str:
    """跑 git 命令返回 stdout；非零退出抛 InvalidInputError（坏 ref/非 git 库等，用户可修正）。"""
    r = await asyncio.to_thread(_run_git, repo_path, list(args))
    if r.returncode != 0:
        raise InvalidInputError(f"git {' '.join(args)} 执行失败: {r.stderr.strip()[:300]}",
                                agent_type="code_review")
    return r.stdout


def _new_entry(path: str, deleted_file: bool) -> dict:
    return {"path": path, "added": 0, "deleted": 0, "hunks": [],
            "binary": False, "deleted_file": deleted_file}


def _parse_diff_text(out: str) -> list[dict]:
    """解析 unified diff 文本为逐文件原始条目（含 binary/超宽，过滤由 _filter_entries 负责）。"""
    entries: list[dict] = []
    cur: dict | None = None
    old_path = ""
    for line in out.splitlines():
        if line.startswith("diff --git "):
            cur, old_path = None, ""
            continue
        if line.startswith("--- "):
            old_path = line[4:].strip()
            if old_path.startswith("a/"):
                old_path = old_path[2:]
            continue
        if line.startswith("+++ "):
            new_path = line[4:].strip()
            if new_path == "/dev/null":
                cur = _new_entry(old_path, deleted_file=True)  # 删除文件：路径与行号都取旧侧
            else:
                if new_path.startswith("b/"):
                    new_path = new_path[2:]
                cur = _new_entry(new_path, deleted_file=False)
            entries.append(cur)
            continue
        if line.startswith("Binary files "):
            # 二进制文件无 ---/+++ 头，路径从本行解析（Binary files a/x and b/x differ）
            if cur is None:
                body = line[len("Binary files "):]
                if body.endswith(" differ"):
                    body = body[: -len(" differ")]
                newp = body.split(" and ")[-1]
                if newp.startswith("b/"):
                    newp = newp[2:]
                cur = _new_entry(newp, deleted_file=False)
                entries.append(cur)
            cur["binary"] = True
            continue
        if cur is None or cur["binary"]:
            continue
        if line.startswith("GIT binary patch"):
            cur["binary"] = True
            continue
        m = _HUNK_RE.match(line)
        if m:
            old_start, old_cnt = int(m.group(1)), int(m.group(2) if m.group(2) else 1)
            new_start, new_cnt = int(m.group(3)), int(m.group(4) if m.group(4) else 1)
            if cur["deleted_file"]:
                start, cnt = old_start, old_cnt
            else:
                start, cnt = new_start, new_cnt
            cur["hunks"].append({"start_line": start,
                                 "end_line": max(start, start + cnt - 1),
                                 "text": line, "_lines": [line]})
            continue
        if not cur["hunks"]:
            continue
        h = cur["hunks"][-1]
        h["_lines"].append(line)
        if line.startswith("+"):
            cur["added"] += 1
        elif line.startswith("-"):
            cur["deleted"] += 1
        # 上下文行与 "\ No newline" 标记只进 text 不计数
    for e in entries:
        for h in e["hunks"]:
            h["text"] = "\n".join(h.pop("_lines"))
    return entries


def _filter_entries(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """binary/lockfile/超宽 进 skipped；其余裁剪内部字段后进 diff_files。"""
    diff_files, skipped = [], []
    for e in entries:
        if e["binary"]:
            skipped.append({"path": e["path"], "reason": "二进制文件"})
        elif _is_lock(e["path"]):
            skipped.append({"path": e["path"], "reason": "lockfile"})
        elif e["added"] + e["deleted"] > MAX_FILE_CHANGES:
            skipped.append({"path": e["path"], "reason": f"变更过宽(>{MAX_FILE_CHANGES}行)"})
        else:
            diff_files.append({"path": e["path"], "added": e["added"],
                               "deleted": e["deleted"], "hunks": e["hunks"]})
    return diff_files, skipped


def _read_untracked(repo_path: str, rel_paths: list[str]) -> tuple[list[dict], list[dict]]:
    """把 untracked 新文件合成「整文件新增」变更条目；不可审的进 skipped。"""
    entries, skipped = [], []
    for rel in rel_paths:
        full = os.path.join(repo_path, rel)
        if _is_lock(rel):
            skipped.append({"path": rel, "reason": "lockfile"})
            continue
        try:
            if os.path.getsize(full) > MAX_UNTRACKED_BYTES:
                skipped.append({"path": rel, "reason": "文件过大"})
                continue
            with open(full, "rb") as fh:
                if b"\x00" in fh.read(8192):
                    skipped.append({"path": rel, "reason": "二进制文件"})
                    continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            skipped.append({"path": rel, "reason": "读取失败"})
            continue
        if len(lines) > MAX_FILE_CHANGES:
            skipped.append({"path": rel, "reason": f"变更过宽(>{MAX_FILE_CHANGES}行)"})
            continue
        entries.append({"path": rel, "added": len(lines), "deleted": 0,
                        "hunks": [{"start_line": 1, "end_line": max(1, len(lines)),
                                   "text": "\n".join([f"@@ -0,0 +1,{len(lines)} @@"]
                                                     + ["+" + l for l in lines])}]})
    return entries, skipped


async def collect_diff(repo_path: str, mode: str,
                       base_ref: str = "", head_ref: str = "") -> dict:
    """三模式取 diff 并解析，返回 {"diff_files": [...], "diff_stats": {...}}。
    失败抛 InvalidInputError（坏 ref/非 git 库/缺参数）；空 diff 返回空列表，由调用方拦截。"""
    if mode == "uncommitted":
        out = await _git(repo_path, "diff", "HEAD", *DIFF_FLAGS)
    elif mode == "range":
        if not base_ref or not head_ref:
            raise InvalidInputError("range 模式需同时提供 base_ref 与 head_ref",
                                    agent_type="code_review")
        out = await _git(repo_path, "diff", f"{base_ref}..{head_ref}", *DIFF_FLAGS)
    elif mode == "commit":
        if not head_ref:
            raise InvalidInputError("commit 模式需提供 head_ref（待审提交）",
                                    agent_type="code_review")
        out = await _git(repo_path, "show", "--format=", "--patch",
                         "-m", "--first-parent", head_ref, *DIFF_FLAGS)
    else:
        raise InvalidInputError(f"不支持的审查模式: {mode}", agent_type="code_review")

    diff_files, skipped = _filter_entries(_parse_diff_text(out))

    if mode == "uncommitted":
        ls = await _git(repo_path, "ls-files", "--others", "--exclude-standard")
        rels = [p.strip() for p in ls.splitlines() if p.strip()]
        if rels:
            un_entries, un_skipped = await asyncio.to_thread(_read_untracked, repo_path, rels)
            diff_files.extend(un_entries)
            skipped.extend(un_skipped)

    stats = {"files": len(diff_files),
             "added": sum(f["added"] for f in diff_files),
             "deleted": sum(f["deleted"] for f in diff_files),
             "skipped": skipped}
    logger.info("code_review.diff_collected", mode=mode, files=stats["files"],
                added=stats["added"], deleted=stats["deleted"], skipped=len(skipped))
    return {"diff_files": diff_files, "diff_stats": stats}
