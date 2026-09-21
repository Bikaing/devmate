# backend/mcp/client.py
# MCP 客户端统一出口：进程内 FastMCP 直连（不启独立子进程，SDK memory stream 走完整协议路径）。
# 职责：server 注册表、会话管理、超时控制、异常转换。
# 异常契约：工具以 JSON 信封回传业务错误（ok=false + error_type），client 还原为原异常类保留 retryable 语义；
# 协议/传输/超时等未知失败统一转 MCPToolError（retryable=True），交给 core/retry.py 按契约处理。
import asyncio
import json

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from backend.core import exceptions as _exc
from backend.core.exceptions import CodePilotBaseError, MCPToolError
from backend.core.logger import get_logger

logger = get_logger(__name__)

CALL_TIMEOUT = 30  # 单次工具调用超时（秒）；外网波动由重试契约吸收

_SERVERS: dict[str, FastMCP] = {}

# 信封错误还原白名单：只有这些业务异常允许跨工具边界还原，其余一律退化 MCPToolError
_ERROR_TYPES: dict[str, type[CodePilotBaseError]] = {
    name: getattr(_exc, name) for name in (
        "InvalidInputError", "RepoNotIndexedError", "MCPToolError",
        "AgentExecutionError", "DatabaseError", "MilvusConnectionError",
    )
}


def register_server(name: str, server: FastMCP) -> None:
    """server 模块 import 时自注册；重名属编程错误，直接失败暴露。"""
    if name in _SERVERS:
        raise ValueError(f"MCP server 重复注册: {name}")
    _SERVERS[name] = server


async def call_mcp_tool(server_name: str, tool_name: str, args: dict,
                        timeout: float = CALL_TIMEOUT) -> object:
    """调用 MCP 工具，返回工具 JSON 信封中的 data 字段（已反序列化）。"""
    server = _SERVERS.get(server_name)
    if server is None:
        raise MCPToolError(f"MCP server 未注册: {server_name}",
                           details={"tool": tool_name})

    async def _call():
        async with create_connected_server_and_client_session(server) as session:
            res = await session.call_tool(tool_name, arguments=args)
        if res.isError:  # 工具内未捕获异常被 FastMCP 转成错误结果：视为传输级失败
            text = "".join(getattr(c, "text", "") for c in res.content)
            raise MCPToolError(f"MCP 工具执行异常: {text[:300]}",
                               details={"server": server_name, "tool": tool_name})
        for block in res.content:
            if getattr(block, "type", "") == "text":
                return json.loads(block.text)
        raise MCPToolError("MCP 工具无文本返回",
                           details={"server": server_name, "tool": tool_name})

    try:
        envelope = await asyncio.wait_for(_call(), timeout=timeout)
    except CodePilotBaseError:
        raise  # 信封还原出的业务异常与 MCPToolError 原样透传
    except asyncio.TimeoutError as e:
        raise MCPToolError(f"MCP 工具调用超时: {server_name}.{tool_name}",
                           details={"timeout": timeout}) from e
    except Exception as e:  # noqa: BLE001 传输/解析等未知失败统一可重试
        logger.error("mcp.call_failed", server=server_name, tool=tool_name, error=str(e))
        raise MCPToolError(f"MCP 工具调用失败: {server_name}.{tool_name}: {e}",
                           details={"server": server_name, "tool": tool_name}) from e

    if not envelope.get("ok"):
        cls = _ERROR_TYPES.get(envelope.get("error_type", ""), MCPToolError)
        # raise 行保持纯 ASCII：3.11.0 traceback 格式化对含中文源码行会 UnicodeDecodeError
        raise cls(envelope.get("message", "MCP tool returned business error"),
                  details={"server": server_name, "tool": tool_name})
    return envelope.get("data")
