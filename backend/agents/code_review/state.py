# backend/agents/code_review/state.py
# code_review 图状态：一个 TypedDict 贯穿 输入 -> diff 产物 -> 审查产物。
# 所有 list/dict 字段统一 plain dict（findings 亦如此），checkpointer 落库零障碍；
# 审查是单发任务（一次 diff 一轮审查），无多轮对话，故不像 code_qa 带 messages reducer。
#
# findings 元素契约（与 review_findings 表列一一对应）：
#   {file, start_line, end_line, severity, category, title,
#    suggestion, suggested_diff(None 可), source('rule'|'llm')}
#   行号锚定 head 版本（新文件侧），与 diff hunk 行号一致。
from typing import TypedDict


class ReviewState(TypedDict, total=False):
    # ── 输入（orchestrator 流前校验后写入）──
    repo_id: str
    repo_path: str       # 仓库本地绝对路径：git diff / git apply 的工作目录
    mode: str            # uncommitted / range / commit
    base_ref: str        # diff 范围起点；uncommitted 模式为空串
    head_ref: str        # diff 范围终点；uncommitted 模式为空串
    review_basis: str    # 审查依据（业务规则文档+用户附注）；空串表示无依据

    # ── diff 节点产物 ──
    diff_files: list[dict]  # [{path, added, deleted, hunks:[{start_line, end_line, text}]}]
    diff_stats: dict        # {files, added, deleted, skipped:[{path, reason}]}

    # ── rules / context 节点产物 ──
    rule_findings: list[dict]  # 规则预检 findings（source='rule'，suggested_diff=None）
    contexts: dict             # {path: [{path, start_line, end_line, content}]}；未索引/召回失败为空 dict

    # ── review / summarize 节点产物 ──
    findings: list[dict]  # 最终 findings：rule + llm 去重合并后的完整契约结构
    summary: str          # 总体风险摘要（summarize 节点产出）
    error: str | None     # 节点级失败信息；API 层转 error 事件
