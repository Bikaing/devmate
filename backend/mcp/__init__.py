# backend/mcp/__init__.py
# MCP 工具层：import 本包即完成各 server 自注册（副作用导入），调用方统一用 call_mcp_tool。
from backend.mcp import client  # noqa: F401  先建注册表
from backend.mcp import knowledge_base_server, web_search_server  # noqa: F401  注册副作用
from backend.mcp.client import call_mcp_tool, register_server

__all__ = ["call_mcp_tool", "register_server"]
