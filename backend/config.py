# backend/config.py
# 全项目唯一的「配置中心」：从 .env.local 读取所有配置项，供任何模块取用。

from functools import lru_cache  # 标准库装饰器：缓存函数结果，让函数体实际只执行一次

from pydantic_settings import BaseSettings, SettingsConfigDict  # Pydantic 的「配置基类」：能自动从环境变量/.env 文件读取


class Settings(BaseSettings):
    """配置模型：每个类属性对应 .env.local 里的一项配置。
    继承 BaseSettings 后，Pydantic 会自动把同名（大小写不敏感）的配置读进来并转成对应类型。
    """

    # ── 数据库（PostgreSQL）──
    db_host: str = "localhost"   # 主机
    db_port: int = 5433          # 端口；本机 5432 已占用，隔离到 5433
    db_name: str = "copilot"     # 库名
    db_user: str                 # 用户名；没有默认值 = 必填，.env.local 缺了会启动报错
    db_password: str             # 密码；同样必填

    @property
    def database_url(self) -> str:
        """把上面几个散件拼成 SQLAlchemy 需要的连接串（异步 psycopg 驱动）。
        用 @property 装饰后，可像访问属性一样 settings.database_url 取值，不用加括号调用。
        """
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ── Milvus 向量库 ──
    milvus_host: str = "localhost"
    milvus_port: int = 19531     # 本机 19530 已占用，隔离到 19531

    # ── 大模型（DeepSeek）──
    deepseek_api_key: str                                 # 必填：DeepSeek 的 API Key
    deepseek_base_url: str = "https://api.deepseek.com"   # DeepSeek 接口地址
    deepseek_model_chat: str = "deepseek-flash"           # 对话模型名
    deepseek_model_coder: str = "deepseek-flash"          # 代码模型名

    # ── 本地模型权重路径 ──
    reranker_model_path: str = "./models/reranker/bge-reranker-large"    # 精排模型
    classifier_model_path: str = "./models/classifier/all-MiniLM-L6-v2"  # 意图分类模型
    bge_m3_model_path: str = "./models/embedding/bge-m3"                 # 嵌入模型

    # ── JWT 认证 ──
    jwt_secret_key: str                          # 签发登录令牌用的密钥
    jwt_algorithm: str = "HS256"                 # 签名算法
    jwt_access_token_expire_minutes: int = 10080  # 令牌有效期（分钟）

    # ── MCP Server 地址（MCP 章用）──
    filesystem_mcp_url: str = "http://localhost:8000/mcp/filesystem"
    git_mcp_url: str = "http://localhost:8000/mcp/git"
    shell_docker_mcp_url: str = "http://localhost:8000/mcp/shell-docker"
    sast_mcp_url: str = "http://localhost:8000/mcp/sast"

    # ── Web 搜索（博查：国内可达的带 key 搜索 API；留空则联网搜索工具报配置缺失并降级）──
    bocha_api_key: str = ""

    # ── 应用基础配置 ──
    app_env: str = "local"       # 运行环境标识
    app_debug: bool = False      # 是否调试模式
    app_host: str = "0.0.0.0"    # 监听地址
    app_port: int = 8000         # 监听端口
    log_level: str = "INFO"      # 日志级别

    # Pydantic 元配置：告诉 BaseSettings 该怎么读取配置
    model_config = SettingsConfigDict(
        env_file=".env.local",      # 从这个文件读取配置
        env_file_encoding="utf-8",  # 文件编码
        case_sensitive=False,       # 大小写不敏感：环境变量 DB_HOST 能对应字段 db_host
        extra="ignore",             # .env.local 里多出来的、模型没定义的字段一律忽略
    )


@lru_cache()
def get_settings() -> Settings:
    """获取全局唯一的配置对象。任何模块要用配置，都调用这个函数。"""
    return Settings()  # 首次调用时创建实例；之后每次都返回同一个缓存对象
