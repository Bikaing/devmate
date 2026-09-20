# backend/agents/code_qa/schema.py
# Code QA 模块的建表 DDL（纯常量文件，禁止 import 任何重量级依赖）。
# 由 backend/db/migrations.py 在启动时集中收集并幂等执行。
#
# 父子分块设计：
#   父块 = 整文件原文，存在 code_files.content（不另建父块表）；
#   子块 = 检索单元，存在 code_chunks，向量入 Milvus、此处存原文与元数据。
#   因此子块只有 file_id 而没有 parent_id——当前父块粒度就是文件本身，
#   parent_id 会恒等于 file_id（冗余列）；超大文件在检索期按 chunk_index
#   取兄弟子块窗口，同样不需要父块行。将来若引入固定大小父块，再以增量
#   迁移补 code_parents 表。

MIGRATIONS: list[tuple[str, str]] = [
    # ── 代码文件表：一行 = 仓库中一个被索引的源文件 ──
    (
        "code_qa.code_files 表",
        """
        CREATE TABLE IF NOT EXISTS code_files (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_id      UUID         NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
            path         TEXT         NOT NULL,
            language     VARCHAR(32)  NOT NULL DEFAULT 'text',
            content      TEXT         NOT NULL,
            content_hash VARCHAR(64)  NOT NULL,
            token_count  INT          NOT NULL DEFAULT 0,
            chunk_count  INT          NOT NULL DEFAULT 0,
            indexed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (repo_id, path)
        )
        """,
    ),
    # ── 代码子块表：一行 = 一个检索单元（子块） ──
    (
        "code_qa.code_chunks 表",
        """
        CREATE TABLE IF NOT EXISTS code_chunks (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_id     UUID         NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
            file_id     UUID         NOT NULL REFERENCES code_files (id) ON DELETE CASCADE,
            chunk_index INT          NOT NULL,
            symbol_name VARCHAR(256),
            start_line  INT          NOT NULL,
            end_line    INT          NOT NULL,
            content     TEXT         NOT NULL,
            token_count INT          NOT NULL DEFAULT 0,
            vector_id   VARCHAR(64)  NOT NULL,
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (vector_id)
        )
        """,
    ),
    # 按仓库圈定检索范围（隔离轴 repo_id 的落地索引）
    (
        "code_qa.idx_code_chunks_repo 索引",
        "CREATE INDEX IF NOT EXISTS idx_code_chunks_repo ON code_chunks (repo_id)",
    ),
    # 增量索引按文件删旧、检索期按文件取兄弟窗口，都走 (file_id, chunk_index)
    (
        "code_qa.idx_code_chunks_file_seq 索引",
        "CREATE INDEX IF NOT EXISTS idx_code_chunks_file_seq ON code_chunks (file_id, chunk_index)",
    ),
    # 触发器 + 列注释合并进一个 DO 块，保证单条语句幂等
    (
        "code_qa.code_files 触发器与列注释",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_code_files_updated_at') THEN
                CREATE TRIGGER trg_code_files_updated_at
                    BEFORE UPDATE ON code_files
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
            END IF;

            COMMENT ON TABLE  code_files IS '代码文件表：content 即检索父块（整文件原文）';
            COMMENT ON COLUMN code_files.content IS '父块本体：检索命中子块后回溯此列喂给 LLM';
            COMMENT ON COLUMN code_files.content_hash IS '文件内容 sha256：增量索引快速判定是否变化';
            COMMENT ON COLUMN code_files.token_count IS '父块 token 数：超阈值时检索改用兄弟子块窗口';
            COMMENT ON COLUMN code_files.chunk_count IS '子块数：索引完整性对账用';

            COMMENT ON TABLE  code_chunks IS '代码子块表：检索单元；向量在 Milvus，此处存原文与元数据';
            COMMENT ON COLUMN code_chunks.chunk_index IS '文件内顺序号：超大文件的兄弟窗口扩展依据';
            COMMENT ON COLUMN code_chunks.symbol_name IS '正则提取的所属符号名（def/class/function），可空';
            COMMENT ON COLUMN code_chunks.vector_id IS 'Milvus 主键：PG 行与向量的对账凭据';
        END
        $$;
        """,
    ),
]
