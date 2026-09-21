# backend/agents/doc_insight/state.py
# doc_insight 图状态：一个 TypedDict 贯穿 输入 -> 分块/提炼中间产物 -> 报告。
# 单发任务无多轮会话（不需要 messages reducer）；chunks/chunk_notes 用 plain 类型，
# checkpointer 落库零障碍；thread_id 取 task_id，checkpoint 仅供排障回看。
from typing import TypedDict


class DocInsightState(TypedDict, total=False):
    # ── 输入（orchestrator 写入）──
    title: str
    content: str          # 文档原文（MVP 为 md/txt 纯文本）

    # ── ingest 节点产物 ──
    chunks: list[str]     # 按段落边界切出的文本块（含少量重叠防跨块信息丢失）

    # ── map 节点产物 ──
    chunk_notes: list[dict]  # [{index, note}]；单块提炼失败降级跳过，不中断任务

    # ── reduce 节点产物 ──
    report: str           # Markdown 结构化报告：概述/关键结论/风险点/行动建议
    error: str | None     # 节点级失败信息；orchestrator 判任务 failed
