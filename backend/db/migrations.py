# backend/db/migrations.py
# 幂等的自动迁移补丁：init_db.sql 只在容器首次启动时执行一次，
# 之后的增量 Schema（底座表加字段、各 Agent 专用表）统一在启动时自动执行。
# 模式：DDL 分布式定义（各 Agent 模块的 schema.py 纯常量）+ 集中式收集执行（本文件）。
from sqlalchemy import text

from backend.agents.code_qa import schema as code_qa_schema
from backend.agents.code_review import schema as code_review_schema
from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal

logger = get_logger(__name__)

# 底座表（init_db.sql）之后的通用补丁，按时间顺序追加。SQL 必须幂等
_CORE_MIGRATIONS: list[tuple[str, str]] = [
    # 格式：("表名.列名 或 索引名", "ALTER TABLE xxx ADD COLUMN IF NOT EXISTS yyy JSONB"),
]

# 各 Agent 模块的 schema 常量模块；新增 Agent 时在此追加，列表顺序即执行顺序。
# 约束：schema.py 只放 SQL 常量，禁止 import 重量级依赖（避免拖慢启动与循环导入）
_AGENT_SCHEMA_MODULES = [
    code_qa_schema,
    code_review_schema,
]

# 合并后的完整补丁列表：先通用补丁，后各 Agent 表
_MIGRATIONS: list[tuple[str, str]] = [
    *_CORE_MIGRATIONS,
    *(entry for mod in _AGENT_SCHEMA_MODULES for entry in mod.MIGRATIONS),
]


async def run_migrations() -> None:
    """应用启动时执行所有 Schema 补丁；单条失败只警告，不阻断启动。"""
    async with AsyncSessionLocal() as session:
        for desc, sql in _MIGRATIONS:
            try:
                await session.execute(text(sql))
                await session.commit()
            except Exception as e:
                await session.rollback()
                # 幂等 SQL 重跑报 already exists 属正常，静默跳过
                if "already exists" not in str(e):
                    logger.warning("db.migration_failed", column=desc, error=str(e))
    logger.info("db.migrations_done", count=len(_MIGRATIONS))
