# backend/agents/code_qa/retriever.py
# 父子检索：Milvus dense+sparse 混合召回(RRF) -> PG 取子块原文 -> Reranker 精排
# -> 按父块(文件)投票加权去重 -> oversized 走兄弟窗口 -> token 预算截断 -> 带 citations 输出。
import asyncio
import uuid
from dataclasses import dataclass, field

from pymilvus import AnnSearchRequest, RRFRanker
from sqlalchemy import text

from backend.agents.code_qa.indexer import OVERSIZED_TOKENS, _estimate_tokens
from backend.core.knowledge_base import COLLECTION_NAME, Embedder, Reranker, VectorStore
from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal

logger = get_logger(__name__)

RECALL_TOP_K = 20             # 每路召回条数，RRF 融合后仍取 top-K
RERANK_TOP_N = 5              # 精排后保留的子块数
VOTE_BONUS = 0.1              # 同文件多子块命中加权：file_score = 最佳分 + VOTE_BONUS*(票数-1)
CONTEXT_TOKEN_BUDGET = 6000   # 喂 LLM 的上下文总 token 预算
SIBLING_WINDOW = 2            # oversized 文件按 chunk_index 向两侧扩展的兄弟子块数


@dataclass(slots=True)
class Citation:
    """引用来源：messages.citations 数组元素的直接数据源。"""
    path: str
    start_line: int
    end_line: int
    symbol: str | None


@dataclass(slots=True)
class RetrievedContext:
    path: str
    language: str
    content: str      # 喂 LLM 的父块文本：整文件，或 oversized 时的兄弟窗口拼接
    token_count: int
    citations: list[Citation] = field(default_factory=list)


def _hybrid_recall(repo_id: str, dense: list[float], sparse: dict[int, float]) -> list[str]:
    """dense(COSINE/HNSW) 与 sparse(IP/倒排) 各召回 RECALL_TOP_K 条再 RRF 融合；expr 做仓库隔离。"""
    expr = f'repo_id == "{repo_id}"'
    reqs = [
        AnnSearchRequest(data=[dense], anns_field="dense",
                         param={"metric_type": "COSINE", "params": {"ef": 64}},
                         limit=RECALL_TOP_K, expr=expr),
        AnnSearchRequest(data=[sparse], anns_field="sparse",
                         param={"metric_type": "IP"},
                         limit=RECALL_TOP_K, expr=expr),
    ]
    res = VectorStore.get_client().hybrid_search(
        collection_name=COLLECTION_NAME, reqs=reqs, ranker=RRFRanker(60),
        limit=RECALL_TOP_K, output_fields=["vector_id"])
    # hybrid_search 的 output_fields 嵌套在 hit["entity"] 下（与普通 search 不同）
    return [h["entity"]["vector_id"] for h in res[0]]


async def retrieve(repo_id: str, query: str,
                   budget: int = CONTEXT_TOKEN_BUDGET) -> list[RetrievedContext]:
    """code_qa 检索入口：返回按相关性排序的父块上下文（带 citations）；无命中返回空列表。"""
    emb = (await asyncio.to_thread(Embedder.encode, [query]))[0]
    vids = await asyncio.to_thread(_hybrid_recall, repo_id, emb.dense, emb.sparse)
    if not vids:
        return []

    # 子块原文与父块元数据都在 PG：Milvus 只出主键和行号
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            "SELECT c.vector_id, c.chunk_index, c.start_line, c.end_line, c.symbol_name, c.content, "
            "f.id AS file_id, f.path, f.language, f.token_count AS file_tokens, f.content AS file_content "
            "FROM code_chunks c JOIN code_files f ON f.id = c.file_id "
            "WHERE c.vector_id = ANY(:vids)"), {"vids": vids})).all()
    if not rows:
        return []
    row_by_vid = {r.vector_id: r for r in rows}

    # 精排：CrossEncoder 对「query-子块」交叉编码，重排 Milvus 的粗召回顺序
    order = await asyncio.to_thread(
        Reranker.rerank, query, [row_by_vid[v].content for v in vids], RERANK_TOP_N)

    # 按父块(文件)投票加权去重：同文件命中子块越多，file_score 越高
    file_hits: dict[uuid.UUID, list[tuple[float, object]]] = {}
    for idx, score in order:
        r = row_by_vid[vids[idx]]
        file_hits.setdefault(r.file_id, []).append((score, r))
    ranked_files = sorted(
        file_hits.items(),
        key=lambda kv: max(s for s, _ in kv[1]) + VOTE_BONUS * (len(kv[1]) - 1),
        reverse=True)

    out: list[RetrievedContext] = []
    used = 0
    async with AsyncSessionLocal() as session:
        for file_id, scored in ranked_files:
            top = scored[0][1]
            if top.file_tokens <= OVERSIZED_TOKENS:
                content, tokens = top.file_content, top.file_tokens
            else:
                # oversized：兄弟子块窗口定行号区间，再切父块原文；
                # 不直接拼子块 content（相邻块含 overlap，拼接会重复文本甚至超整文件）
                lo = min(r.chunk_index for _, r in scored) - SIBLING_WINDOW
                hi = max(r.chunk_index for _, r in scored) + SIBLING_WINDOW
                sib = (await session.execute(text(
                    "SELECT start_line, end_line FROM code_chunks WHERE file_id = :fid "
                    "AND chunk_index BETWEEN :lo AND :hi ORDER BY chunk_index"),
                    {"fid": file_id, "lo": lo, "hi": hi})).all()
                lines = top.file_content.splitlines()
                lo_line = min(x.start_line for x in sib) if sib else top.start_line
                hi_line = max(x.end_line for x in sib) if sib else top.end_line
                content = "\n".join(lines[lo_line - 1:hi_line])
                tokens = _estimate_tokens(content)
            if used + tokens > budget:
                if not out:          # 至少保一条上下文：按预算截断
                    content = content[:budget * 4]
                    tokens = budget
                else:
                    break
            used += tokens
            cites: list[Citation] = []
            seen: set[tuple[int, int]] = set()
            for _, r in scored:      # citations 行号取子块精确区间
                if (r.start_line, r.end_line) not in seen:
                    seen.add((r.start_line, r.end_line))
                    cites.append(Citation(top.path, r.start_line, r.end_line, r.symbol_name))
            out.append(RetrievedContext(path=top.path, language=top.language,
                                        content=content, token_count=tokens, citations=cites))

    logger.info("code_qa.retrieved", repo_id=repo_id, files=len(out), tokens=used)
    return out
