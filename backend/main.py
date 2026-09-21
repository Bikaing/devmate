# backend/main.py
# FastAPI 应用入口：lifespan 内完成 日志配置 -> 幂等迁移 -> PG checkpointer -> 编译各 Agent 图，
# 图挂在 app.state 上全生命周期复用（编译产物无状态，会话隔离靠 thread_id）。
# 启动：uvicorn backend.main:app --port 8000
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.v1 import auth, code_qa, code_review, doc_insight, repos
from backend.agents.code_qa.graph import build_code_qa_graph
from backend.agents.code_review.graph import build_code_review_graph
from backend.agents.doc_insight.graph import build_doc_insight_graph
from backend.config import get_settings
from backend.core.logger import configure_logging, get_logger
from backend.core.memory import get_checkpointer
from backend.db.migrations import run_migrations

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()                       # 全局日志只在这里配一次
    await run_migrations()                    # 幂等 Schema 补丁
    async with get_checkpointer() as ckpt:    # PG 检查点器，进程级复用
        app.state.code_qa_graph = build_code_qa_graph(checkpointer=ckpt)
        app.state.code_review_graph = build_code_review_graph(checkpointer=ckpt)
        app.state.doc_insight_graph = build_doc_insight_graph(checkpointer=ckpt)
        logger.info("app.graphs_ready")
        yield


app = FastAPI(title="DevMate", version="0.1.0", lifespan=lifespan)

# 本地开发放行所有来源；上生产收紧为前端域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1/auth")
app.include_router(code_qa.router, prefix="/api/v1")
app.include_router(code_review.router, prefix="/api/v1")
app.include_router(doc_insight.router, prefix="/api/v1")
app.include_router(repos.router, prefix="/api/v1")


@app.get("/healthz")
async def healthz():
    """存活探针。"""
    return {"status": "ok", "app_env": get_settings().app_env}
