# backend/agents/code_review/schema.py
# Code Review 模块的建表 DDL（纯常量文件，禁止 import 任何重量级依赖）。
# 由 backend/db/migrations.py 在启动时集中收集并幂等执行。
#
# 两表设计（HitL 修复闭环）：
#   review_tasks    = 一次审查请求（diff/commit 范围 + 状态 + 总体摘要）；
#   review_findings = 审查发现的问题，一条 = 一个 finding：
#     suggestion     文字建议（必填，供人阅读判断 LLM 说得对不对）；
#     suggested_diff unified diff 补丁（可空，规则预检/告知类无补丁）；
#     hitl_status    由人决定是否应用补丁：pending -> applied / rejected。
#   行号锚定 head 版本（新文件侧），与 diff hunk 的新文件行号一致。

MIGRATIONS: list[tuple[str, str]] = [
    # ── 审查任务表：一行 = 一次审查请求 ──
    (
        "code_review.review_tasks 表",
        """
        CREATE TABLE IF NOT EXISTS review_tasks (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_id       UUID         NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
            user_id       UUID         NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            mode          VARCHAR(16)  NOT NULL
                          CHECK (mode IN ('uncommitted', 'range', 'commit')),
            base_ref      VARCHAR(255),
            head_ref      VARCHAR(255),
            review_basis  TEXT,
            diff_stats    JSONB        NOT NULL DEFAULT '{}'::jsonb,
            status        VARCHAR(16)  NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'success', 'failed')),
            summary       TEXT,
            error_message TEXT,
            duration_ms   INTEGER,
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """,
    ),
    # ── 审查发现表：一行 = 一个问题（文字建议 + 可选补丁 + HitL 状态） ──
    (
        "code_review.review_findings 表",
        """
        CREATE TABLE IF NOT EXISTS review_findings (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id        UUID         NOT NULL REFERENCES review_tasks (id) ON DELETE CASCADE,
            file           TEXT         NOT NULL,
            start_line     INTEGER      NOT NULL,
            end_line       INTEGER      NOT NULL,
            severity       VARCHAR(16)  NOT NULL
                           CHECK (severity IN ('critical', 'warning', 'info')),
            category       VARCHAR(16)  NOT NULL
                           CHECK (category IN ('bug', 'security', 'perf', 'style', 'test')),
            title          TEXT         NOT NULL,
            suggestion     TEXT         NOT NULL,
            suggested_diff TEXT,
            hitl_status    VARCHAR(16)  NOT NULL DEFAULT 'pending'
                           CHECK (hitl_status IN ('pending', 'applied', 'rejected')),
            applied_commit VARCHAR(64),
            source         VARCHAR(8)   NOT NULL DEFAULT 'llm'
                           CHECK (source IN ('rule', 'llm')),
            created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            CHECK (start_line >= 1 AND end_line >= start_line)
        )
        """,
    ),
    # 按仓库回看历史审查任务
    (
        "code_review.idx_review_tasks_repo 索引",
        "CREATE INDEX IF NOT EXISTS idx_review_tasks_repo ON review_tasks (repo_id)",
    ),
    # 用户的任务列表按时间倒序
    (
        "code_review.idx_review_tasks_user_time 索引",
        "CREATE INDEX IF NOT EXISTS idx_review_tasks_user_time ON review_tasks (user_id, created_at DESC)",
    ),
    # 任务详情页按 task 拉全部 findings
    (
        "code_review.idx_review_findings_task 索引",
        "CREATE INDEX IF NOT EXISTS idx_review_findings_task ON review_findings (task_id)",
    ),
    # 待确认队列：只扫 pending 的 finding（应用/拒绝工作台）
    (
        "code_review.idx_review_findings_pending 索引",
        "CREATE INDEX IF NOT EXISTS idx_review_findings_pending ON review_findings (task_id) "
        "WHERE hitl_status = 'pending'",
    ),
    # 触发器 + 列注释合并进一个 DO 块，保证单条语句幂等
    (
        "code_review.review 表触发器与列注释",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_review_tasks_updated_at') THEN
                CREATE TRIGGER trg_review_tasks_updated_at
                    BEFORE UPDATE ON review_tasks
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
            END IF;

            COMMENT ON TABLE  review_tasks IS '审查任务：一次审查请求（diff 范围 + 状态 + 总体摘要）';
            COMMENT ON COLUMN review_tasks.mode IS '审查输入模式：uncommitted 未提交 / range base..head / commit 单提交';
            COMMENT ON COLUMN review_tasks.base_ref IS 'diff 范围起点；uncommitted 模式为 NULL';
            COMMENT ON COLUMN review_tasks.head_ref IS 'diff 范围终点；uncommitted 模式为 NULL';
            COMMENT ON COLUMN review_tasks.review_basis IS '审查依据：业务规则文档内容+用户附注需求描述，注入 prompt 作业务逻辑审查基准；NULL 表示无依据';
            COMMENT ON COLUMN review_tasks.diff_stats IS 'diff 统计：{files, added, deleted, skipped}';
            COMMENT ON COLUMN review_tasks.summary IS 'LLM 产出的总体风险摘要';
            COMMENT ON COLUMN review_tasks.error_message IS '失败原因，status=failed 时填充';

            COMMENT ON TABLE  review_findings IS '审查发现：一个问题一行，文字建议+可选补丁，HitL 决定是否应用';
            COMMENT ON COLUMN review_findings.file IS '问题所在文件（仓库相对路径），行号锚定 head 版本';
            COMMENT ON COLUMN review_findings.suggestion IS '文字建议摘要：哪里有问题/为什么/怎么改，供人核对 LLM 判断';
            COMMENT ON COLUMN review_findings.suggested_diff IS 'unified diff 补丁，可空：规则预检/告知类 finding 无补丁';
            COMMENT ON COLUMN review_findings.hitl_status IS '人在回路确认状态：pending 待确认 / applied 已应用 / rejected 已拒绝';
            COMMENT ON COLUMN review_findings.applied_commit IS '补丁应用后的 commit hash，未应用为 NULL';
            COMMENT ON COLUMN review_findings.source IS '发现来源：rule 规则预检 / llm 模型审查';
        END
        $$;
        """,
    ),
]
