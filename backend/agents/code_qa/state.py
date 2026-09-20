# backend/agents/code_qa/state.py
# code_qa 图状态：一个 TypedDict 贯穿 输入 -> 检索中间产物 -> 输出产物。
# contexts/citations 统一用 plain dict（retriever dataclass 经 asdict 序列化），
# checkpointer 落库零障碍；多轮对话靠 messages + checkpointer 按 thread_id 恢复。
from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class CodeQAState(TypedDict, total=False):
    # ── 输入（API 层写入）──
    repo_id: str
    query: str                                    # 本轮问题，retrieve 节点直接消费
    messages: Annotated[list[Any], add_messages]  # 多轮消息流，generate 节点追加 AIMessage

    # ── retrieve 节点产物 ──
    contexts: list[dict]   # RetrievedContext 的 asdict 形态：path/language/content/token_count/citations

    # ── generate 节点产物 ──
    answer: str
    citations: list[dict]  # [{path, start_line, end_line, symbol}]，messages.citations 字段的直接数据源
    error: str | None      # 节点级失败信息；API 层转 msg_type='error' 消息
