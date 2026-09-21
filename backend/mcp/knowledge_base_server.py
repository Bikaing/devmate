# backend/mcp/knowledge_base_server.py
# 知识库 MCP Server：把代码检索能力（retriever.retrieve，Milvus 混合召回+rerank）以标准工具对外暴露。
# 校验：仓库存在（InvalidInputError）+ 已索引（RepoNotIndexedError）；
# 归属校验留在调用方（MCP 层无用户上下文）。业务错误走 JSON 信封回传，client 还原异常类。
import json

from mcp.server.fastmcp import FastMCP
from sqlalchemy import text

from backend.agents.code_qa.retriever import CONTEXT_TOKEN_BUDGET, retrieve
from backend.core.exceptions import CodePilotBaseError, InvalidInputError, RepoNotIndexedError
from backend.dependencies import AsyncSessionLocal
from backend.mcp.client import register_server

server = FastMCP("knowledge_base")

# 文案常量：raise 行保持纯 ASCII（3.11.0 traceback 对含中文源码行会 UnicodeDecodeError）
MSG_REPO_MISSING = "仓库不存在"
MSG_REPO_NOT_INDEXED = "仓库索引未就绪"


def _err(e: CodePilotBaseError) -> str:
    return json.dumps({"ok": False, "error_type": type(e).__name__, "message": str(e)},
                      ensure_ascii=False)


@server.tool()
async def search_knowledge_base(repo_id: str, query: str,
                                budget: int = CONTEXT_TOKEN_BUDGET) -> str:
    """检索代码知识库（语义+稀疏混合召回、rerank 精排）。
    返回 JSON 信封：data = [{path, language, content, token_count,
    citations: [{path, start_line, end_line, symbol}]}]，行号可寻址。"""
    try:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(text(
                "SELECT index_status FROM repos WHERE id = :rid"),
                {"rid": repo_id})).first()
        if row is None:
            raise InvalidInputError(MSG_REPO_MISSING, details={"repo_id": repo_id})
        if row.index_status != "indexed":
            raise RepoNotIndexedError(MSG_REPO_NOT_INDEXED,
                                      details={"repo_id": repo_id, "status": row.index_status})
        ctxs = await retrieve(repo_id, query, budget)
        data = [{
            "path": c.path,
            "language": c.language,
            "content": c.content,
            "token_count": c.token_count,
            "citations": [{"path": ci.path, "start_line": ci.start_line,
                           "end_line": ci.end_line, "symbol": ci.symbol}
                          for ci in c.citations],
        } for c in ctxs]
        return json.dumps({"ok": True, "data": data}, ensure_ascii=False)
    except CodePilotBaseError as e:
        return _err(e)


register_server("knowledge_base", server)
