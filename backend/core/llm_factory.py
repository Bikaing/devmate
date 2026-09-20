# backend/core/llm_factory.py
# LLM Factory：统一封装大模型调用，按 Agent 类型路由。
# 所有 Agent 必须通过此模块获取模型，禁止直接调用 init_chat_model。
from typing import Any, Type

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── 自定义 httpx 客户端：绕过系统代理 ──
# Windows 系统代理或 HTTPS_PROXY 环境变量会被 httpx 默认探测到，
# 导致 DeepSeek 请求经代理后 TLS 握手失败；trust_env=False 完全忽略系统代理。
_HTTP_ASYNC_CLIENT = httpx.AsyncClient(
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),  # 总超时 120 秒，建立连接超时 15 秒
)
_HTTP_SYNC_CLIENT = httpx.Client(
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),
)

# ── Agent 类型 → 模型标识符 的路由表 ──
# 给某类业务换模型，只改这里一行即可。
_AGENT_MODEL_ROUTING: dict[str, str] = {
    "code_qa": "deepseek-coder",         # 代码库问答：代码理解为主
    "code_review": "deepseek-coder",     # 代码审查：安全/逻辑审查 + 修复 diff
    "doc_insight": "deepseek-chat",      # 文档洞察：长文提炼与报告生成
    "incident_triage": "deepseek-chat",  # 故障排查：日志推理与假设验证
    "summarize": "deepseek-chat",        # 对话摘要压缩（预留）
}


class LLMFactory:
    """大模型工厂（统一获取模型的唯一入口）。"""

    _instances: dict[str, BaseChatModel] = {}  # 模型实例缓存（缓存键 → 模型）

    @classmethod
    def _get_settings(cls):
        """取配置对象。"""
        return get_settings()

    @classmethod
    def _model_id_map(cls) -> dict[str, str]:
        """模型标识符 → DeepSeek API 实际接受的 model 名称（来自 .env.local）。"""
        settings = cls._get_settings()
        return {
            "deepseek-chat": settings.deepseek_model_chat,
            "deepseek-coder": settings.deepseek_model_coder,
        }

    @classmethod
    def _build_model_kwargs(cls, model_key: str) -> dict[str, Any]:
        """内部方法：组装 init_chat_model 需要的所有参数（DeepSeek 走 OpenAI 兼容接口）。"""
        settings = cls._get_settings()
        model_id = cls._model_id_map()[model_key]
        return {
            "model": model_id,
            "model_provider": "openai",       # 强制走 openai 兼容接口（DeepSeek 兼容）
            "temperature": 0,                 # 默认 0：审查/修复/评分要稳定输出
            "api_key": settings.deepseek_api_key,
            "base_url": settings.deepseek_base_url,
            "max_retries": 0,                 # 模型层不重试；重试统一由 retry.py 负责
            "http_async_client": _HTTP_ASYNC_CLIENT,
            "http_client": _HTTP_SYNC_CLIENT,
        }

    @classmethod
    def get_llm(
        cls,
        agent_type: str,
        temperature: float = 0,
        streaming: bool = False,
    ) -> BaseChatModel:
        """按 Agent 类型获取模型实例（带缓存）。
        相同（模型，温度，是否流式）的组合只会创建一次，之后复用。"""
        if agent_type not in _AGENT_MODEL_ROUTING:
            raise ValueError(
                f"未知 agent_type: '{agent_type}', "
                f"可用类型: {list(_AGENT_MODEL_ROUTING.keys())}"
            )
        model_key = _AGENT_MODEL_ROUTING[agent_type]

        # 用「模型_温度_是否流式」拼一个缓存键：不同组合各缓存一份
        cache_key = f"{model_key}_{temperature}_{streaming}"
        if cache_key not in cls._instances:
            kwargs = cls._build_model_kwargs(model_key)
            kwargs["temperature"] = temperature
            kwargs["streaming"] = streaming
            llm = init_chat_model(**kwargs)
            cls._instances[cache_key] = llm
            logger.info(
                "llm_factory.model_initialized",
                agent_type=agent_type, model_key=model_key,
                temperature=temperature, streaming=streaming,
            )
        return cls._instances[cache_key]

    @classmethod
    def get_structured_llm(
        cls,
        agent_type: str,
        output_schema: Type[BaseModel],
        temperature: float = 0,
    ) -> Runnable:
        """获取「绑定了结构化输出 Schema」的模型。"""
        llm = cls.get_llm(agent_type, temperature=temperature)
        # 绑定 Pydantic 结构；method="function_calling" 是 DeepSeek 必须的
        return llm.with_structured_output(output_schema, method="function_calling")

    @classmethod
    def clear_cache(cls) -> None:
        """清空模型实例缓存。"""
        cls._instances.clear()
        logger.info("llm_factory.cache_cleared")


def get_llm(agent_type: str, temperature: float = 0, streaming: bool = False) -> BaseChatModel:
    """LLMFactory.get_llm 的便捷入口。"""
    return LLMFactory.get_llm(agent_type, temperature=temperature, streaming=streaming)


def get_structured_llm(agent_type: str, output_schema: Type[BaseModel]) -> Runnable:
    """LLMFactory.get_structured_llm 的便捷入口。"""
    return LLMFactory.get_structured_llm(agent_type, output_schema)
