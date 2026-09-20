# backend/core/logger.py
# 全项目的日志工具：在标准库 logging 之上做一层包装，
# 支持「事件名 + 键值对」的结构化日志写法。

import logging
import sys

from backend.config import get_settings


class _Logger:
    """日志包装类：支持 logger.info("事件名", key=value) 的结构化写法，
    内部仍委托给标准库 logging 输出。外部通过 get_logger() 获取实例。"""

    def __init__(self, name: str):
        # name 通常传 __name__，日志里能看出是哪个模块打的
        self._log = logging.getLogger(name)

    def _fmt(self, event: str, **kw) -> str:
        """把「事件名 + 关键字参数」格式化成一行：event | k='v' k2='v2'"""
        if kw:
            return event + " | " + " ".join(f"{k}={v!r}" for k, v in kw.items())
        return event

    def debug(self, event: str, **kw):
        self._log.debug(self._fmt(event, **kw))

    def info(self, event: str, *args, **kw):
        # 兼容两种写法：info("事件", k=v) 与 info("模板 %s", 值)
        if args:
            self._log.info(event, *args)
        else:
            self._log.info(self._fmt(event, **kw))

    def warning(self, event: str, *args, **kw):
        if args:
            self._log.warning(event, *args)
        else:
            self._log.warning(self._fmt(event, **kw))

    def error(self, event: str, **kw):
        # 支持 exc_info=True 把异常堆栈一并打印
        exc_info = kw.pop("exc_info", False)
        self._log.error(self._fmt(event, **kw), exc_info=exc_info)

    def exception(self, event: str, **kw):
        # 对齐标准库 logging.exception：except 块内用，自动带当前异常堆栈
        self._log.exception(self._fmt(event, **kw))

    def critical(self, event: str, **kw):
        self._log.critical(self._fmt(event, **kw))


def configure_logging() -> None:
    """全局日志配置：整个应用只在启动时（main.py）调用一次。"""
    settings = get_settings()
    # 把字符串级别转成 logging 常量，配置写错时兜底 INFO
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
        level=level,
        force=True,  # 强制覆盖已有配置，避免重复调用 basicConfig 失效
    )
    # 压低第三方库的 INFO 噪音，避免刷屏
    for noisy in ("sqlalchemy.engine", "sqlalchemy.pool", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> _Logger:
    """对外工厂函数：每个模块用 get_logger(__name__) 拿到自己的日志器。"""
    return _Logger(name)
