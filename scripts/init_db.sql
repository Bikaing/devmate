-- =========================================================
-- init_db.sql —— 公共底座表（初始 schema）
-- 作用：创建 users / repos / conversations / agent_runs / messages
--       五张底座表，以及 updated_at 自动更新触发器函数
-- 用法：Docker 环境无需手动执行（容器首次启动经 docker-entrypoint-initdb.d 自动跑）；
--       非 Docker 环境：psql -h localhost -p 5433 -U copilot_user -d copilot -f scripts/init_db.sql
-- 约定：主键统一 UUID（PG13+ 内置 gen_random_uuid()）；
--       业务隔离轴为 repos.id（repo_id）；
--       Agent 专用表（code_qa / code_review 等）与后续加字段都不在此写，
--       统一以幂等 SQL 补丁追加到 backend/db/migrations.py 的 _MIGRATIONS 列表
-- =========================================================

BEGIN;

-- ---------- 触发器函数：updated_at 自动刷新 ----------
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------- 1. users：鉴权主体 ----------
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      VARCHAR(64)  NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    display_name  VARCHAR(64),
    role          VARCHAR(16)  NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE users IS '鉴权主体：登录用户，JWT 签发对象';

-- ---------- 2. repos：代码仓库登记（业务隔离轴） ----------
CREATE TABLE IF NOT EXISTS repos (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID         NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name                VARCHAR(128) NOT NULL,
    path                TEXT         NOT NULL,
    git_remote          TEXT,
    default_branch      VARCHAR(128) NOT NULL DEFAULT 'main',
    index_status        VARCHAR(16)  NOT NULL DEFAULT 'pending'
                        CHECK (index_status IN ('pending', 'indexing', 'indexed', 'failed')),
    last_indexed_commit VARCHAR(64),
    last_indexed_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, path)
);
COMMENT ON TABLE  repos IS '代码仓库登记：业务父表/隔离轴，MCP 路径白名单依据';
COMMENT ON COLUMN repos.path IS '本地仓库绝对路径，先登记后开放给 FileSystem/Git MCP';
COMMENT ON COLUMN repos.last_indexed_commit IS '增量索引锚点：上次扫描到的 commit';
CREATE INDEX IF NOT EXISTS idx_repos_user_id ON repos (user_id);

-- ---------- 3. conversations：会话容器 ----------
CREATE TABLE IF NOT EXISTS conversations (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID         NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title          VARCHAR(255) NOT NULL DEFAULT '新会话',
    agent_type     VARCHAR(32)  NOT NULL DEFAULT 'unified'
                   CHECK (agent_type IN ('unified', 'code_qa', 'code_review', 'doc_insight', 'incident_triage')),
    thread_id      VARCHAR(128) NOT NULL UNIQUE,
    status         VARCHAR(16)  NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    last_active_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE  conversations IS '会话容器：会话列表/历史回看/HitL 恢复的业务入口';
COMMENT ON COLUMN conversations.thread_id IS 'LangGraph checkpoint 线程 ID，状态恢复靠它';
COMMENT ON COLUMN conversations.agent_type IS '入口 Agent，unified 为统一对话入口';
CREATE INDEX IF NOT EXISTS idx_conversations_user_active ON conversations (user_id, last_active_at DESC);

-- ---------- 4. agent_runs：任务级执行记录 ----------
-- 注：先于 messages 创建，供 messages.agent_run_id 外键引用
CREATE TABLE IF NOT EXISTS agent_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   UUID        NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    repo_id           UUID        REFERENCES repos (id) ON DELETE SET NULL,
    agent_type        VARCHAR(32) NOT NULL
                      CHECK (agent_type IN ('code_qa', 'code_review', 'doc_insight', 'incident_triage')),
    status            VARCHAR(16) NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'awaiting_confirm', 'success', 'failed')),
    input_summary     TEXT,
    error_message     TEXT,
    prompt_tokens     INTEGER     NOT NULL DEFAULT 0,
    completion_tokens INTEGER     NOT NULL DEFAULT 0,
    duration_ms       INTEGER,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE  agent_runs IS 'Agent 执行记录：观测/重试统计/HitL 恢复/评估入口，一次会话 1:N';
COMMENT ON COLUMN agent_runs.status IS 'awaiting_confirm = HitL 中断待确认，进程重启后可找回';
CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation ON agent_runs (conversation_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_unfinished ON agent_runs (status)
    WHERE status IN ('running', 'awaiting_confirm');

-- ---------- 5. messages：消息流水（展示与审计） ----------
-- 只追加不更新，无 updated_at 与触发器
CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID        NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    agent_run_id    UUID        REFERENCES agent_runs (id) ON DELETE SET NULL,
    role            VARCHAR(16) NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    msg_type        VARCHAR(32) NOT NULL DEFAULT 'text'
                    CHECK (msg_type IN ('text', 'diff', 'confirm_card', 'code_block', 'error')),
    content         TEXT        NOT NULL,
    citations       JSONB,
    token_count     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE  messages IS '消息流水：只追加，负责前端渲染与审计；状态恢复由 checkpoint 承担';
COMMENT ON COLUMN messages.agent_run_id IS '由哪次执行产出，用户消息为 NULL';
COMMENT ON COLUMN messages.citations IS '引用来源数组：[{file, start_line, end_line, symbol}]';
COMMENT ON COLUMN messages.msg_type IS '前端卡片类型：文本/diff/HitL 确认卡片/代码块/错误';
CREATE INDEX IF NOT EXISTS idx_messages_conversation_time ON messages (conversation_id, created_at);

-- ---------- updated_at 触发器挂载（仅可变表） ----------
DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_repos_updated_at ON repos;
CREATE TRIGGER trg_repos_updated_at BEFORE UPDATE ON repos
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_conversations_updated_at ON conversations;
CREATE TRIGGER trg_conversations_updated_at BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_agent_runs_updated_at ON agent_runs;
CREATE TRIGGER trg_agent_runs_updated_at BEFORE UPDATE ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMIT;
