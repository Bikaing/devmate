# backend/core/retry.py
# 三层兜底机制：自动重试 → Agent 级降级 → 系统级兜底
import asyncio
from functools import wraps
from typing import Any, Callable, Optional

from backend.core.exceptions import CodePilotBaseError
from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── 重试策略常量 ──
MAX_RETRIES = 2              # 最多重试 2 次（加上首次 = 共 3 次尝试）
RETRY_DELAYS = [1.0, 3.0]    # 第 1 次重试前等 1 秒，第 2 次前等 3 秒
TIMEOUT_PER_ATTEMPT = 30  # 单次调用最多等 30 秒，与 llm_factory 的 httpx 总超时对齐

# 编程错误类：重试无意义（输入/逻辑问题，不会自愈），立即抛出
_PROGRAMMING_ERRORS = (ValueError, TypeError, KeyError, AttributeError)


def _is_retryable(exc: Exception) -> bool:
    """重试决策：自定义异常看 retryable 属性（exceptions.py 的契约）；
    编程错误立即失败；其余（超时/连接/第三方 SDK 瞬时故障）默认可重试。"""
    if isinstance(exc, CodePilotBaseError):
        return exc.retryable
    if isinstance(exc, _PROGRAMMING_ERRORS):
        return False
    return True


def with_retry(agent_type: str = ""):
    """三层兜底装饰器工厂：给异步函数套上「重试 → 降级 → 系统兜底」三层保护。

    用法：
        @with_retry(agent_type="code_qa")
        async def _invoke():
            return await graph.ainvoke(state, config=config)
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            # ── 第一层：自动重试 ──
            last_error: Optional[Exception] = None  # 记录最后一次的错误，留给后面降级用
            for attempt in range(MAX_RETRIES + 1):
                try:
                    # 给单次调用套一个超时：超过 TIMEOUT_PER_ATTEMPT 就抛 TimeoutError
                    result = await asyncio.wait_for(
                        func(*args, **kwargs),
                        timeout=TIMEOUT_PER_ATTEMPT,
                    )
                    if attempt > 0:  # 重试后才成功的，记一条日志
                        logger.info("retry.succeeded", agent_type=agent_type, attempt=attempt)
                    return result
                except Exception as e:
                    if not _is_retryable(e):  # 不可重试：立即抛出，交给上层处理
                        logger.warning("retry.non_retryable_error", agent_type=agent_type, error=str(e))
                        raise
                    last_error = e
                    if attempt < MAX_RETRIES:  # 还没到上限：等待后重试
                        delay = RETRY_DELAYS[attempt]
                        logger.warning(
                            "retry.attempt_failed", agent_type=agent_type,
                            attempt=attempt + 1, max_retries=MAX_RETRIES, delay=delay, error=str(e),
                        )
                        await asyncio.sleep(delay)
                    else:  # 到上限了：记录失败，跳出循环去降级
                        logger.error("retry.all_attempts_failed", agent_type=agent_type, error=str(e))

            # ── 第二层：Agent 级降级 ──
            try:
                fallback_result = await AgentFallbackHandler.handle(
                    agent_type=agent_type, original_error=last_error,
                )
                logger.info("retry.fallback_succeeded", agent_type=agent_type)
                return fallback_result  # 降级成功，返回降级结果
            except Exception as fallback_error:  # 连降级都失败
                logger.error("retry.fallback_failed", agent_type=agent_type, error=str(fallback_error))

            # ── 第三层：系统级兜底 ──
            logger.error("retry.system_fallback", agent_type=agent_type, original_error=str(last_error))
            return _system_fallback_response(agent_type)  # 最后的保底，永远不会再失败

        return wrapper

    return decorator


class AgentFallbackHandler:
    """第二层降级：各 Agent 的专项降级策略（尽量保留核心功能，退化为更简单的实现）。"""

    @classmethod
    async def handle(cls, agent_type: str, original_error: Exception) -> Any:
        """根据 agent_type 选择对应的降级策略。"""
        fallback_map = {
            "code_qa": cls._code_qa_fallback,
            "code_review": cls._code_review_fallback,
            "doc_insight": cls._doc_insight_fallback,
            "incident_triage": cls._incident_triage_fallback,
        }
        handler = fallback_map.get(agent_type)
        if handler:
            return await handler()
        raise original_error  # 没有对应降级策略，原样抛出（交给系统兜底）

    @classmethod
    async def _code_qa_fallback(cls) -> dict:
        """QA 降级：代码库检索或模型不可用，返回提示语。"""
        logger.info("fallback.code_qa_service_unavailable")
        return {
            "fallback_used": True,
            "content": "⚠️ 代码库检索暂时不可用，请稍后重试，或确认目标仓库索引已构建（index_status=indexed）。",
            "structured_output": None,
        }

    @classmethod
    async def _code_review_fallback(cls) -> dict:
        """审查降级：标记需人工复核，提交不丢失。"""
        logger.info("fallback.code_review_needs_human")
        return {
            "fallback_used": True,
            "needs_human_review": True,
            "fallback_note": "AI 审查服务暂时不可用，已标记为需人工复核。",
        }

    @classmethod
    async def _doc_insight_fallback(cls) -> dict:
        """文档洞察降级：提示服务不可用 / 检查文件。"""
        logger.info("fallback.doc_insight_service_unavailable")
        return {
            "fallback_used": True,
            "content": "文档洞察服务暂时不可用，请稍后重试。如持续失败，请检查上传的 PDF/Word 文件是否损坏。",
            "structured_output": None,
        }

    @classmethod
    async def _incident_triage_fallback(cls) -> dict:
        """故障分诊降级：故障记录已保存，稍后重试。"""
        logger.info("fallback.incident_triage_record_saved")
        return {
            "fallback_used": True,
            "content": "故障分诊服务暂时不可用，本次故障记录已保存，请稍后重试。",
            "structured_output": None,
        }


def _system_fallback_response(agent_type: str) -> dict:
    """第三层：系统级兜底。所有降级都失败后返回它，保证用户始终能收到响应。"""
    messages = {
        "code_qa": "非常抱歉，代码问答服务暂时不可用，请稍后再试。",
        "code_review": "非常抱歉，代码审查服务暂时不可用，您的提交已保存，待服务恢复后将自动处理。",
        "doc_insight": "非常抱歉，文档洞察服务暂时不可用，请稍后重新上传。",
        "incident_triage": "非常抱歉，故障分诊服务暂时不可用，请稍后重新开始。",
    }
    content = messages.get(agent_type, "服务暂时不可用，请稍后重试。")
    return {
        "messages": [],
        "content": content,
        "fallback_used": True,
        "system_fallback": True,  # 标记：走到了最后一层系统兜底
        "structured_output": None,
    }
