# backend/agents/code_review/prompts.py
# code_review 提示词：审查人设 + 变更/上下文序列化 + 结构化输出 schema。
# 字符串模板保持轻量不引 langchain；pydantic schema 仅供 get_structured_llm 作输出契约。
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

SYSTEM_PROMPT = """你是 DevMate，一名资深代码审查工程师，审查代码变更（diff）并指出真实风险。
规则：
1. 只审查变更行；行号一律用 diff 新文件侧（head 版本），与 hunk 行号一致，禁止编造行号。
2. 每个问题输出一条 finding：severity 取 critical（崩溃/安全漏洞/数据错误）、warning（边界条件/逻辑缺陷/性能隐患）、info（可读性/可维护性）；category 取 bug/security/perf/style/test。
3. suggestion 用文字说清：哪里有问题、为什么是问题、怎么改，供读者核对你的判断。
4. suggested_diff 给出可在 head 版本直接 git apply 的 unified diff 补丁，改法必须与 suggestion 口径一致；问题不适合自动补丁（需讨论、跨文件大重构）时为 null。
5. 提供审查依据（业务规则）时逐条对照检查；违反依据的问题 severity 不低于 warning，并在 suggestion 中引用对应规则。
6. 相关上下文仅用于理解调用关系与约定，不要对上下文中未变更的代码提 finding。
7. 不吹毛求疵挑风格；没有真实问题就返回空列表，不要硬凑。"""

NO_CONTEXT_HINT = "(未提供相关代码上下文：仅基于 diff 本身审查，不要臆测调用关系。)"

BASIS_BLOCK = """审查依据（业务规则，逐条对照）：
{basis}

"""

HUNK_BLOCK = """--- hunk 新文件行 L{start}-L{end} ---
{text}"""

FILE_BLOCK = """## 文件: {path} (+{added} -{deleted})
{hunks}"""

CONTEXT_BLOCK = """--- 相关上下文 {i}: {path} L{start}-L{end} ---
{content}"""

USER_TEMPLATE = """{basis}{file_block}

{contexts}

请审查以上变更，按结构化格式输出 findings。"""

SUMMARIZE_TEMPLATE = """变更统计：{files} 个文件，+{added} -{deleted}，跳过 {skipped} 个文件。
共发现 {total} 个问题（critical {critical} / warning {warning} / info {info}）：
{listing}

请输出总体风险摘要：变更意图、主要风险点、结论建议（200 字内）。"""


class FindingModel(BaseModel):
    """单条审查发现：字段语义与 review_findings 表列一一对应。"""
    file: str = Field(description="问题所在文件（仓库相对路径，与 diff 中路径一致）")
    start_line: int = Field(ge=1, description="问题区间起行（head 版本新文件行号）")
    end_line: int = Field(ge=1, description="问题区间止行，须 >= start_line")
    severity: Literal["critical", "warning", "info"]
    category: Literal["bug", "security", "perf", "style", "test"]
    title: str = Field(description="一句话问题标题")
    suggestion: str = Field(description="文字建议：哪里有问题/为什么/怎么改")
    suggested_diff: Optional[str] = Field(
        default=None, description="可在 head 版本 git apply 的 unified diff 补丁；不适合自动补丁时为 null")

    @model_validator(mode="after")
    def _check_line_range(self) -> "FindingModel":
        # 与 review_findings 表 CHECK (start_line>=1 AND end_line>=start_line) 对齐，解析期拦截
        if self.end_line < self.start_line:
            raise ValueError(f"end_line({self.end_line}) < start_line({self.start_line})")
        return self


class ReviewOutputModel(BaseModel):
    """逐文件审查的结构化输出：无问题即空列表。"""
    findings: list[FindingModel] = Field(default_factory=list)


class SummaryModel(BaseModel):
    """总体摘要的结构化输出。"""
    summary: str = Field(description="总体风险摘要：变更意图、主要风险点、结论建议")


def format_hunks(hunks: list[dict]) -> str:
    """把单文件的 hunk 列表序列化为提示词文本块。"""
    return "\n".join(HUNK_BLOCK.format(start=h["start_line"], end=h["end_line"], text=h["text"])
                     for h in hunks)


def format_file(f: dict) -> str:
    """把单个变更文件序列化为提示词文本块（路径 + 增删统计 + 各 hunk）。"""
    return FILE_BLOCK.format(path=f["path"], added=f["added"], deleted=f["deleted"],
                             hunks=format_hunks(f["hunks"]))


def format_contexts(contexts: list[dict]) -> str:
    """把某文件的相关上下文块序列化；空列表返回 NO_CONTEXT_HINT 供无上下文分支使用。"""
    if not contexts:
        return NO_CONTEXT_HINT
    blocks = []
    for i, c in enumerate(contexts, 1):
        blocks.append(CONTEXT_BLOCK.format(i=i, path=c["path"], start=c["start_line"],
                                           end=c["end_line"], content=c["content"]))
    return "\n\n".join(blocks)


def build_user_prompt(f: dict, contexts: list[dict], basis: str = "") -> str:
    """组装单文件审查的 User 提示词：审查依据（可空）+ 文件变更块 + 相关上下文。"""
    basis_text = BASIS_BLOCK.format(basis=basis) if basis.strip() else ""
    return USER_TEMPLATE.format(basis=basis_text, file_block=format_file(f),
                                contexts=format_contexts(contexts))


def build_summarize_prompt(diff_stats: dict, findings: list[dict]) -> str:
    """组装总体摘要的 User 提示词：统计 + findings 清单（最多列 50 条控长度）。"""
    listing = "\n".join(
        f"- [{f['severity']}][{f['category']}] {f['file']} "
        f"L{f['start_line']}-L{f['end_line']}: {f['title']}"
        for f in findings[:50]) or "(无)"
    sev = {k: sum(1 for f in findings if f["severity"] == k)
           for k in ("critical", "warning", "info")}
    return SUMMARIZE_TEMPLATE.format(
        files=diff_stats.get("files", 0), added=diff_stats.get("added", 0),
        deleted=diff_stats.get("deleted", 0), skipped=len(diff_stats.get("skipped") or []),
        total=len(findings), listing=listing, **sev)
