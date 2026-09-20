# backend/core/memory.py
# LangGraph 检查点管理：按 thread_id 持久化会话状态（跨请求的会话记忆）。
# 约定：thread_id 取自 conversations.thread_id；Agent 一律通过本模块拿 checkpointer 编译图。
import asyncio
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

# Windows 兼容：psycopg 异步模式跑不了 ProactorEventLoop（Windows 默认事件循环），
# 必须切到 SelectorEventLoop；Linux/macOS 上此分支不生效。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _checkpoint_dsn() -> str:
    """检查点库用 psycopg 直连数据库，不认 SQLAlchemy 的驱动后缀，
    所以把 database_url 的 postgresql+psycopg:// 还原成 postgresql://。"""
    return get_settings().database_url.replace("+psycopg", "", 1)


@asynccontextmanager
async def get_checkpointer() -> AsyncGenerator[AsyncPostgresSaver, None]:
    """异步上下文管理器：产出 PG 检查点器，退出时自动关连接。
    setup() 幂等建表（checkpoint_migrations / checkpoints / checkpoint_blobs / checkpoint_writes），
    首次调用创建，之后调用跳过。

    用法（应用 lifespan 里创建一次，全生命周期复用）：
        async with get_checkpointer() as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
    """
    async with AsyncPostgresSaver.from_conn_string(_checkpoint_dsn()) as saver:
        await saver.setup()
        logger.info("memory.checkpointer_ready")
        yield saver


def get_in_memory_checkpointer() -> MemorySaver:
    """内存版检查点器：单测或 PG 不可用时用，进程退出状态即丢失。"""
    return MemorySaver()


def make_config(thread_id: str, **extra: Any) -> dict:
    """构造 LangGraph invoke/ainvoke 需要的 config：绑定 thread_id（即 conversations.thread_id）。"""
    return {"configurable": {"thread_id": thread_id, **extra}}
