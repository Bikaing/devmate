# backend/mcp/web_search_server.py
# Web 搜索 MCP Server：包装博查 Web Search API（国内可达、带 key），为各 Agent 提供联网能力。
# httpx 原生异步直调；失败走信封 MCPToolError（retryable=True）由重试契约吸收。
import json

import httpx
from mcp.server.fastmcp import FastMCP

from backend.config import get_settings
from backend.core.exceptions import InvalidInputError, MCPToolError
from backend.core.logger import get_logger
from backend.mcp.client import register_server

logger = get_logger(__name__)

server = FastMCP("web_search")

MAX_RESULTS_LIMIT = 10  # 单次搜索条数硬上限，防 prompt 注入式超量请求
BOCHA_URL = "https://api.bochaai.com/v1/web-search"
HTTP_TIMEOUT = 15.0  # 单次 HTTP 超时；外层另有 MCP CALL_TIMEOUT=30 兜底

# 文案常量：raise 行保持纯 ASCII（3.11.0 traceback 对含中文源码行会 UnicodeDecodeError）
MSG_EMPTY_QUERY = "搜索关键词不能为空"
MSG_NO_API_KEY = "未配置博查 API key（.env.local 的 BOCHA_API_KEY）"


def _err_envelope(error_type: str, message: str) -> str:
    return json.dumps({"ok": False, "error_type": error_type, "message": message},
                      ensure_ascii=False)


@server.tool()
async def web_search(query: str, max_results: int = 5) -> str:
    """博查网页搜索。返回 JSON 信封：data = [{title, href, body}]。"""
    if not query or not query.strip():
        return _err_envelope("InvalidInputError", MSG_EMPTY_QUERY)
    api_key = get_settings().bocha_api_key
    if not api_key:
        # 配置缺失不可重试：用 InvalidInputError 语义，避免重试契约空转
        return _err_envelope(InvalidInputError.__name__, MSG_NO_API_KEY)
    n = max(1, min(max_results, MAX_RESULTS_LIMIT))

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                BOCHA_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"query": query, "summary": True, "count": n},
            )
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 外网波动/超时：可重试
        logger.error("mcp.web_search_failed", query=query[:80], error=str(e))
        return _err_envelope(MCPToolError.__name__, f"web 搜索失败: {e}")

    if resp.status_code != 200 or payload.get("code") != 200:
        # 401/403/配额等业务拒绝：可重试语义交给上层按 retryable 判断
        msg = f"博查返回异常: http={resp.status_code} code={payload.get('code')} msg={payload.get('msg')}"
        logger.error("mcp.web_search_rejected", query=query[:80], detail=msg[:300])
        return _err_envelope(MCPToolError.__name__, msg[:300])

    pages = (payload.get("data") or {}).get("webPages") or {}
    rows = pages.get("value") or []
    data = [{"title": r.get("name", ""), "href": r.get("url", ""),
             "body": r.get("summary") or r.get("snippet", "")} for r in rows[:n]]
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False)


register_server("web_search", server)
