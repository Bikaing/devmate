"""修复任务与轮次 DDL。纯常量，由启动迁移在 review schema 之后执行。

任务归属通过 finding -> review_tasks -> user/repo 获取，不重复存储归属字段。
验证证据采用 JSONB 数组，元素契约见 state.VerificationResult。
"""

MIGRATIONS: list[tuple[str, str]] = [
    (
        "code_fix.fix_tasks 表",
        """
        CREATE TABLE IF NOT EXISTS fix_tasks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            finding_id UUID NOT NULL REFERENCES review_findings (id) ON DELETE CASCADE,
            finding_snapshot JSONB NOT NULL,
            review_basis TEXT,
            base_commit VARCHAR(64),
            snapshot_id TEXT,
            workspace_path TEXT,
            status VARCHAR(16) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'preparing', 'fixing', 'validating',
                                  'ready', 'failed', 'cancelled', 'applied')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
            patch TEXT,
            changed_files JSONB NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(changed_files) = 'array'),
            baseline_results JSONB NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(baseline_results) = 'array'),
            verification_status VARCHAR(16) NOT NULL DEFAULT 'not_run'
                CHECK (verification_status IN ('not_run', 'passed', 'failed', 'insufficient', 'error')),
            verification_results JSONB NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(verification_results) = 'array'),
            verification_summary TEXT,
            error_message TEXT,
            applied_commit VARCHAR(64),
            duration_ms INTEGER CHECK (duration_ms >= 0),
            finished_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (attempt_count >= 0 AND attempt_count <= max_attempts)
        )
        """,
    ),
    (
        "code_fix.fix_attempts 表",
        """
        CREATE TABLE IF NOT EXISTS fix_attempts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id UUID NOT NULL REFERENCES fix_tasks (id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            status VARCHAR(16) NOT NULL DEFAULT 'fixing'
                CHECK (status IN ('fixing', 'validating', 'completed', 'failed', 'cancelled')),
            patch TEXT,
            changed_files JSONB NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(changed_files) = 'array'),
            verification_status VARCHAR(16) NOT NULL DEFAULT 'not_run'
                CHECK (verification_status IN ('not_run', 'passed', 'failed', 'insufficient', 'error')),
            verification_results JSONB NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(verification_results) = 'array'),
            verification_summary TEXT,
            error_message TEXT,
            model_calls INTEGER NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
            prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
            completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
            duration_ms INTEGER CHECK (duration_ms >= 0),
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (task_id, attempt_number)
        )
        """,
    ),
    (
        "code_fix.idx_fix_tasks_finding_time 索引",
        "CREATE INDEX IF NOT EXISTS idx_fix_tasks_finding_time "
        "ON fix_tasks (finding_id, created_at DESC)",
    ),
    # ready 尚待用户处置，也占据活跃槽位，避免同一问题有多个待应用补丁。
    (
        "code_fix.idx_fix_tasks_active_finding 唯一索引",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_tasks_active_finding "
        "ON fix_tasks (finding_id) "
        "WHERE status IN ('pending', 'preparing', 'fixing', 'validating', 'ready')",
    ),
    (
        "code_fix.updated_at 触发器",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_fix_tasks_updated_at') THEN
                CREATE TRIGGER trg_fix_tasks_updated_at BEFORE UPDATE ON fix_tasks
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_fix_attempts_updated_at') THEN
                CREATE TRIGGER trg_fix_attempts_updated_at BEFORE UPDATE ON fix_attempts
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
            END IF;
            COMMENT ON TABLE fix_tasks IS '针对审查 finding 的修复任务；ready 待审阅，applied 已应用';
            COMMENT ON TABLE fix_attempts IS '修复轮次与验证证据；completed 不代表验证通过';
            COMMENT ON COLUMN fix_tasks.snapshot_id IS '包含未提交修改的输入代码快照标识，由工作区模块生成';
            COMMENT ON COLUMN fix_tasks.patch IS '相对输入快照的累计补丁';
            COMMENT ON COLUMN fix_attempts.patch IS '本轮结束时相对输入快照的累计补丁';
        END
        $$;
        """,
    ),
]

# 兼容已经执行过第一步 DDL 的数据库。确认应用只修改工作树，不自动提交。
MIGRATIONS += [
    # 兼容历史独立规划记录；新修复循环不使用 analysis / analyzing。
    ('code_fix.analysis',
     'ALTER TABLE fix_attempts ADD COLUMN IF NOT EXISTS analysis JSONB'),
    ('code_fix.analysis task status',
     """DO $$ BEGIN ALTER TABLE fix_tasks DROP CONSTRAINT IF EXISTS fix_tasks_status_check;
        ALTER TABLE fix_tasks ADD CONSTRAINT fix_tasks_status_check
        CHECK (status IN ('pending','preparing','analyzing','fixing','validating','ready','failed','cancelled','applied')); END $$;"""),
    ('code_fix.analysis attempt status',
     """DO $$ BEGIN ALTER TABLE fix_attempts DROP CONSTRAINT IF EXISTS fix_attempts_status_check;
        ALTER TABLE fix_attempts ADD CONSTRAINT fix_attempts_status_check
        CHECK (status IN ('analyzing','fixing','validating','completed','failed','cancelled')); END $$;"""),
    ('code_fix.analysis active index',
     """DO $$ BEGIN DROP INDEX IF EXISTS idx_fix_tasks_active_finding;
        CREATE UNIQUE INDEX idx_fix_tasks_active_finding ON fix_tasks (finding_id)
        WHERE status IN ('pending','preparing','analyzing','fixing','validating','ready'); END $$;"""),
    ('code_fix.apply constraint',
     """DO $$ DECLARE constraint_name text; BEGIN
        FOR constraint_name IN SELECT conname FROM pg_constraint
          WHERE conrelid='fix_tasks'::regclass AND contype='c'
            AND pg_get_constraintdef(oid) LIKE '%applied_commit%'
        LOOP
          EXECUTE format('ALTER TABLE fix_tasks DROP CONSTRAINT %I', constraint_name);
        END LOOP;
     END $$;"""),
    ('code_fix.verification_profile',
     "ALTER TABLE fix_tasks ADD COLUMN IF NOT EXISTS verification_profile VARCHAR(16) NOT NULL DEFAULT 'pytest'"),
    ('code_fix.cleanup_error',
     'ALTER TABLE fix_tasks ADD COLUMN IF NOT EXISTS cleanup_error TEXT'),
    ('code_review.input_tree',
     'ALTER TABLE review_tasks ADD COLUMN IF NOT EXISTS input_tree TEXT'),
    ('code_review.input_commit',
     'ALTER TABLE review_tasks ADD COLUMN IF NOT EXISTS input_commit VARCHAR(64)'),
]
