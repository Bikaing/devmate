# backend/agents/code_qa/indexer.py
# 全量索引：扫描 -> 父子切片 -> 批量向量化 -> 双库写入 -> 状态机。
# 父子分块形态：子块入 Milvus 作检索单元；父块 = 整文件原文存 code_files.content 喂 LLM。
# oversized 不另存列：检索期按 code_files.token_count > OVERSIZED_TOKENS 判定，走兄弟窗口。
import asyncio
import hashlib
import os
import re
import subprocess
import uuid
from dataclasses import dataclass

from langchain_text_splitters import Language, RecursiveCharacterTextSplitter
from sqlalchemy import text

from backend.core.exceptions import IndexBuildError
from backend.core.knowledge_base import Embedder, VectorStore
from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal

logger = get_logger(__name__)

CHUNK_SIZE = 300             # 子块目标 token 数（检索单元粒度）
CHUNK_OVERLAP = 50           # 相邻子块重叠 token，防语义截断
MAX_FILE_BYTES = 512 * 1024  # 单文件上限，超大文件（生成物/压缩包误入）直接跳过
OVERSIZED_TOKENS = 2500      # 父块超此 token 数视为 oversized，检索期走兄弟窗口

_SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv",
              "dist", "build", ".idea", ".pytest_cache", ".mypy_cache",
              "models"}  # models 为本地权重目录，其 README/json 入索引只会污染召回

# 扩展名 -> Language 枚举候选名（按序取第一个存在的；空元组 = 通用切片器）
_EXT_LANG: dict[str, tuple[str, ...]] = {
    ".py": ("PYTHON",), ".js": ("JS", "JAVASCRIPT"), ".jsx": ("JS", "JAVASCRIPT"),
    ".ts": ("TS", "TYPESCRIPT"), ".tsx": ("TS", "TYPESCRIPT"),
    ".java": ("JAVA",), ".go": ("GO",), ".md": ("MARKDOWN",),
    ".sql": ("SQL",), ".json": ("JSON",), ".yml": ("YAML",), ".yaml": ("YAML",),
    ".html": ("HTML",), ".css": ("CSS",), ".sh": ("BASH", "SHELL"), ".ps1": ("POWERSHELL",),
    ".txt": (), ".toml": (), ".ini": (), ".cfg": (),
}

# 子块内首个命中的符号名即该块归属符号（def/class/function/export/func）
_SYMBOL_RES = (
    re.compile(r"^\s*(?:async\s+)?def\s+(\w+)", re.M),
    re.compile(r"^\s*class\s+(\w+)", re.M),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?function\s+(\w+)", re.M),
    re.compile(r"^\s*export\s+(?:const|let|var)\s+(\w+)", re.M),
    re.compile(r"^\s*func\s+(?:\([\w\s,*./]+\)\s*)?(\w+)", re.M),
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


@dataclass(slots=True)
class _ScannedFile:
    path: str           # 相对仓库根的路径，正斜杠
    language: str       # 入库语言标识
    content: str
    content_hash: str
    token_count: int


@dataclass(slots=True)
class _Chunk:
    path: str
    language: str
    chunk_index: int
    symbol_name: str | None
    start_line: int
    end_line: int
    content: str
    token_count: int
    vector_id: str      # PG 与 Milvus 共用同一主键，双库对账依据
    file_id: str = ""


@dataclass(slots=True)
class IndexSummary:
    files_scanned: int = 0
    files_indexed: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    chunks_created: int = 0
    total_tokens: int = 0


def _estimate_tokens(t: str) -> int:
    """廉价 token 估算：CJK 一字一 token，其余约 4 字符一 token。够切片与预算用，不引分词器。"""
    cjk = len(_CJK_RE.findall(t))
    return cjk + max(1, (len(t) - cjk) // 4)


def _symbol_name(content: str) -> str | None:
    for rx in _SYMBOL_RES:
        m = rx.search(content)
        if m:
            return m.group(1)[:256]
    return None


_splitters: dict[str, RecursiveCharacterTextSplitter] = {}


def _get_splitter(lang: Language | None) -> RecursiveCharacterTextSplitter:
    key = lang.value if lang else ""
    if key not in _splitters:
        kwargs = dict(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
                      length_function=_estimate_tokens)
        _splitters[key] = (RecursiveCharacterTextSplitter.from_language(lang, **kwargs)
                           if lang else RecursiveCharacterTextSplitter(**kwargs))
    return _splitters[key]


def _resolve_lang(names: tuple[str, ...]) -> Language | None:
    for n in names:
        member = getattr(Language, n, None)
        if member is not None:
            return member
    return None


_LANG_BY_VALUE: dict[str, Language] = {m.value: m for m in Language}


def _split_file(f: _ScannedFile) -> list[_Chunk]:
    """切子块并用游标 find 回定位行号：splitter 只给文本，行号靠在原文中的偏移换算。"""
    texts = _get_splitter(_LANG_BY_VALUE.get(f.language)).split_text(f.content)
    chunks: list[_Chunk] = []
    pos = 0
    for i, ct in enumerate(texts):
        start = f.content.find(ct, pos)
        if start < 0:          # 重叠拼接导致定位失败的兜底：沿用上一游标
            start = pos
        end = start + len(ct)
        chunks.append(_Chunk(
            path=f.path, language=f.language, chunk_index=i,
            symbol_name=_symbol_name(ct),
            start_line=f.content.count("\n", 0, start) + 1,
            end_line=f.content.count("\n", 0, max(start, end - 1)) + 1,
            content=ct, token_count=_estimate_tokens(ct),
            vector_id=str(uuid.uuid4()),
        ))
        pos = start + 1
    return chunks


def _scan_repo(repo_path: str) -> list[_ScannedFile]:
    out: list[_ScannedFile] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in _EXT_LANG:
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                continue
            lang = _resolve_lang(_EXT_LANG[ext])
            out.append(_ScannedFile(
                path=os.path.relpath(full, repo_path).replace(os.sep, "/"),
                language=lang.value if lang else ext.lstrip(".") or "text",
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                token_count=_estimate_tokens(content),
            ))
    return out


def _git_head(repo_path: str) -> str | None:
    """取 HEAD 作为增量索引锚点；非 git 仓库返回 None。"""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_diff_names(repo_path: str, anchor: str) -> list[str] | None:
    """anchor..HEAD 之间变更文件的相对路径列表（posix 斜杠）；失败返回 None 由调用方降级全量。"""
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "diff", "--name-only",
                            f"{anchor}..HEAD"], cwd=repo_path,
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        return [p.strip() for p in r.stdout.splitlines() if p.strip()]
    except (OSError, subprocess.SubprocessError):
        return None


async def _claim_indexing(rid: uuid.UUID) -> tuple[str, str | None]:
    """CAS 抢锁：仅 pending/indexed/failed 可进 indexing，防并发重复建；返回 (仓库路径, 增量锚点)。"""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            "UPDATE repos SET index_status='indexing' "
            "WHERE id = :rid AND index_status IN ('pending','indexed','failed') "
            "RETURNING path, last_indexed_commit"), {"rid": rid})).first()
        if row is None:
            raise IndexBuildError(f"仓库不存在或正在索引中: {rid}")
        await session.commit()
    return row[0], row[1]


async def _run_index(repo_id: str, rid: uuid.UUID, repo_path: str,
                     only_paths: set[str] | None) -> IndexSummary:
    """索引执行体：扫描切片 -> 批量向量化 -> 先删后插写双库 -> 置状态。
    only_paths=None 为全量镜像（含删除文件）；非 None 只处理指定路径（增量）。
    先删后插天然幂等：崩溃重试重跑会再按文件清一次残向量。"""
    # 扫描切片
    summary = IndexSummary()
    try:
        scanned = await asyncio.to_thread(_scan_repo, repo_path)
        if only_paths is not None:
            scanned = [f for f in scanned if f.path in only_paths]
        summary.files_scanned = len(scanned)

        async with AsyncSessionLocal() as session:
            existing = {r.path: (r.id, r.content_hash) for r in (await session.execute(
                text("SELECT id, path, content_hash FROM code_files WHERE repo_id = :rid"),
                {"rid": rid})).all()}

        scanned_paths = {f.path for f in scanned}
        # 增量模式下范围外的文件不参与「消失判定」，避免误删未变更文件
        removed = [(path, fid) for path, (fid, _) in existing.items()
                   if path not in scanned_paths and (only_paths is None or path in only_paths)]
        changed = [f for f in scanned if existing.get(f.path, ("", ""))[1] != f.content_hash]
        summary.files_unchanged = len(scanned) - len(changed)
        summary.files_removed = len(removed)

        # 删除步：磁盘已消失的文件整行删（chunks 级联）；变更文件的旧块在写入步先删后插
        async with AsyncSessionLocal() as session:
            for path, file_id in removed:
                await session.execute(
                    text("DELETE FROM code_files WHERE id = :fid"), {"fid": file_id})
                await asyncio.to_thread(VectorStore.delete_by_file, repo_id, str(file_id))
            await session.commit()

        # 批量向量化
        chunks_by_path: dict[str, list[_Chunk]] = {f.path: _split_file(f) for f in changed}
        all_chunks = [c for cs in chunks_by_path.values() for c in cs]
        embedded = await asyncio.to_thread(Embedder.encode, [c.content for c in all_chunks])

        # 先删后插，chunks写双库
        async with AsyncSessionLocal() as session:
            for f in changed:
                # 1. 写入/更新 code_files，拿到 file_id
                file_id = (await session.execute(text(
                    "INSERT INTO code_files (repo_id, path, language, content, content_hash, token_count, chunk_count) "
                    "VALUES (:rid, :path, :lang, :content, :hash, :tokens, :nchunks) "
                    "ON CONFLICT (repo_id, path) DO UPDATE SET language=EXCLUDED.language, content=EXCLUDED.content, "
                    "content_hash=EXCLUDED.content_hash, token_count=EXCLUDED.token_count, "
                    "chunk_count=EXCLUDED.chunk_count, indexed_at=NOW() RETURNING id"), {
                        "rid": rid, "path": f.path, "lang": f.language, "content": f.content,
                        "hash": f.content_hash, "tokens": f.token_count,
                        "nchunks": len(chunks_by_path[f.path])})).scalar_one()

                # 2. 删除该文件的旧 chunks（PG + Milvus）
                await session.execute(
                    text("DELETE FROM code_chunks WHERE file_id = :fid"), {"fid": file_id})
                await asyncio.to_thread(VectorStore.delete_by_file, repo_id, str(file_id))

                # 3. 把 file_id 回填到 chunk 对象
                for c in chunks_by_path[f.path]:
                    c.file_id = str(file_id)

            # 4. 批量插入新 chunks
            if all_chunks:
                await session.execute(text(
                    "INSERT INTO code_chunks (repo_id, file_id, chunk_index, symbol_name, "
                    "start_line, end_line, content, token_count, vector_id) "
                    "VALUES (:rid, :fid, :idx, :sym, :sline, :eline, :content, :tokens, :vid)"),
                    [{"rid": rid, "fid": uuid.UUID(c.file_id), "idx": c.chunk_index, "sym": c.symbol_name,
                      "sline": c.start_line, "eline": c.end_line, "content": c.content,
                      "tokens": c.token_count, "vid": c.vector_id} for c in all_chunks])
            await session.commit()

        # 5. 批量写入 Milvus
        if all_chunks:
            await asyncio.to_thread(VectorStore.upsert_chunks, [
                {"vector_id": c.vector_id, "repo_id": repo_id, "file_id": c.file_id,
                 "chunk_index": c.chunk_index, "start_line": c.start_line, "end_line": c.end_line,
                 "dense": e.dense, "sparse": e.sparse}
                for c, e in zip(all_chunks, embedded)])

        summary.files_indexed = len(changed)
        summary.chunks_created = len(all_chunks)
        summary.total_tokens = sum(f.token_count for f in changed)

        # 置状态
        head = await asyncio.to_thread(_git_head, repo_path)
        async with AsyncSessionLocal() as session:
            await session.execute(text(
                "UPDATE repos SET index_status='indexed', last_indexed_commit=:head, last_indexed_at=NOW() "
                "WHERE id = :rid AND index_status='indexing'"), {"head": head, "rid": rid})
            await session.commit()
    except Exception:
        async with AsyncSessionLocal() as session:
            await session.execute(text(
                "UPDATE repos SET index_status='failed' WHERE id = :rid AND index_status='indexing'"),
                {"rid": rid})
            await session.commit()
        logger.exception("code_qa.index_failed", repo_id=repo_id)
        raise

    logger.info("code_qa.index_done", repo_id=repo_id, incr=only_paths is not None,
                files=summary.files_indexed, chunks=summary.chunks_created)
    return summary


async def index_repo(repo_id: str) -> IndexSummary:
    """全量索引入口：镜像工作树（含已删除文件）；hash 未变的文件整文件跳过，重跑幂等。"""
    # psycopg3 原生适配 uuid 对象；text() 里写 :x::uuid 会与占位符解析冲突，故不在 SQL 里强转
    rid = uuid.UUID(repo_id)
    repo_path, _ = await _claim_indexing(rid)
    return await _run_index(repo_id, rid, repo_path, None)


async def incremental_index(repo_id: str) -> IndexSummary:
    """增量索引入口：只重建 git diff 锚点..HEAD 变更的文件（文件级先删后插）。
    只认已提交的变更；锚点缺失（非 git 仓库/从未索引）或 git 失败自动降级全量。"""
    rid = uuid.UUID(repo_id)
    repo_path, anchor = await _claim_indexing(rid)
    only_paths: set[str] | None = None
    if anchor:
        names = await asyncio.to_thread(_git_diff_names, repo_path, anchor)
        if names is None:
            logger.warning("code_qa.incr_fallback_full", repo_id=repo_id, anchor=anchor[:8])
        else:
            only_paths = set(names)
    return await _run_index(repo_id, rid, repo_path, only_paths)
