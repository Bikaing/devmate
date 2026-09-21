# backend/agents/doc_insight/schema.py
# Doc Insight 模块的建表 DDL（纯常量文件，禁止 import 任何重量级依赖）。
# 由 backend/db/migrations.py 在启动时集中收集并幂等执行。
#
# 单表设计：doc_tasks = 一次文档洞察任务（原文 + 状态 + 报告）。
#   洞察是单发任务无多轮会话，不进 conversations/messages/agent_runs；
#   MVP 原文直接落 content 列（PG 大文本用 TEXT 不拆表），二期再迁 MinIO。

MIGRATIONS: list[tuple[str, str]] = [
    # ── 洞察任务表：一行 = 一次上传的文档 + 生成的报告 ──
    (
        "doc_insight.doc_tasks 表",
        """
        CREATE TABLE IF NOT EXISTS doc_tasks (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id       UUID         NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            title         VARCHAR(200) NOT NULL,
            content       TEXT         NOT NULL,
            status        VARCHAR(16)  NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'success', 'failed')),
            report        TEXT,
            error_message TEXT,
            duration_ms   INTEGER,
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """,
    ),
    # 用户任务列表按时间倒序（历史抽屉）
    (
        "doc_insight.idx_doc_tasks_user_time 索引",
        "CREATE INDEX IF NOT EXISTS idx_doc_tasks_user_time "
        "ON doc_tasks (user_id, created_at DESC)",
    ),
    # 触发器 + 列注释合并进一个 DO 块，保证单条语句幂等
    (
        "doc_insight.doc_tasks 触发器与列注释",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_doc_tasks_updated_at') THEN
                CREATE TRIGGER trg_doc_tasks_updated_at
                    BEFORE UPDATE ON doc_tasks
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
            END IF;

            COMMENT ON TABLE  doc_tasks IS '文档洞察任务：一次上传的文档原文 + map-reduce 生成的结构化报告';
            COMMENT ON COLUMN doc_tasks.title IS '文档标题（用户填写或前端取文件名）';
            COMMENT ON COLUMN doc_tasks.content IS '文档原文（MVP 直接落库，二期迁 MinIO 后改存对象路径）';
            COMMENT ON COLUMN doc_tasks.report IS 'Markdown 结构化报告：概述/关键结论/风险点/行动建议';
            COMMENT ON COLUMN doc_tasks.error_message IS '失败原因，status=failed 时填充';
        END
        $$;
        """,
    ),
]
