# main.py —— 本地开发启动入口
# 必须经由本文件启动（python main.py），不要直接 python -m uvicorn backend.main:app：
# uvicorn 先创建事件循环、后导入应用模块，dependencies.py 里的 SelectorEventLoop
# 守卫会来不及生效，psycopg 异步直接报 ProactorEventLoop InterfaceError。
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402  （必须在 policy 设置之后导入）

from backend.config import get_settings  # noqa: E402

if __name__ == "__main__":
    s = get_settings()
    uvicorn.run("backend.main:app", host=s.app_host, port=s.app_port)
