# backend/agents/code_qa/prompts.py
# code_qa 提示词：系统人设 + 检索上下文序列化。纯字符串模块，不引 langchain（保持轻量）。

SYSTEM_PROMPT = """你是 DevMate，一名代码仓库问答工程师，只依据提供的仓库上下文回答代码问题。
规则：
1. 代码事实只来自给定上下文，禁止凭记忆编造 API、行号或行为。
2. 上下文不足时：明确说明缺什么、给出验证思路，不要硬答。
3. 涉及代码片段用 Markdown 代码块并标注语言。
4. 回答末尾用 `路径:L起-L止` 格式列出引用行号，符号级命中附符号名。
5. 跟随用户提问的语言作答。
6. 若提供网页搜索结果：通用/时效性问题可参考并列出 URL；代码事实仍以仓库上下文为准。
7. 若标明问题与仓库无关：直接作答，不硬扯仓库、不列仓库引用。"""

ROUTE_SYSTEM_PROMPT = """你是查询意图分类器。判断用户问题是否需要结合当前代码仓库的上下文才能回答。
只输出一个词：
- repo：与当前仓库的代码、实现、配置、依赖版本、运行行为、设计决策有关的问题
（包括对本项目技术选型「为什么这么设计/这么配置」的追问）。
- general：通用技术知识、行业时效信息、工具用法等与当前仓库代码无关的问题。
拿不准时输出 repo（宁可检索落空，不要漏检仓库）。
只输出 repo 或 general，不要输出任何其他内容。"""

GENERAL_CONTEXT_HINT = "(意图判定：本题与仓库代码无关。请基于自身知识与网页搜索结果（若有）直接作答，无需提及仓库。)"

NO_CONTEXT_HINT = "(索引中未命中相关代码：请基于通用知识谨慎回答，并明确告知该回答不基于当前仓库。)"

CONTEXT_BLOCK = """--- 上下文 {i}: {path} ({language}) 命中: {cites} ---
{content}"""

WEB_SOURCE_BLOCK = """[网页 {i}] {title}
URL: {href}
摘要: {body}"""

WEB_SECTION_TEMPLATE = """
网页搜索结果（外部参考，注意甄别时效性）：
{blocks}
"""


def format_web_sources(sources: list[dict]) -> str:
    """网页搜索结果序列化为提示词段落；空列表返回空串（开关 OFF/搜索失败不占位）。"""
    if not sources:
        return ""
    blocks = "\n".join(
        WEB_SOURCE_BLOCK.format(i=i, title=s.get("title", ""),
                                href=s.get("href", ""), body=s.get("body", ""))
        for i, s in enumerate(sources, 1))
    return WEB_SECTION_TEMPLATE.format(blocks=blocks)


def format_contexts(contexts: list[dict]) -> str:
    """把检索结果序列化为提示词文本块；空列表返回 NO_CONTEXT_HINT 供无上下文分支使用。"""
    if not contexts:
        return NO_CONTEXT_HINT
    blocks = []
    for i, c in enumerate(contexts, 1):
        cites = ", ".join(
            f"L{x['start_line']}-L{x['end_line']}" + (f" {x['symbol']}" if x.get("symbol") else "")
            for x in c["citations"]) or "-"
        blocks.append(CONTEXT_BLOCK.format(i=i, path=c["path"], language=c["language"],
                                           cites=cites, content=c["content"]))
    return "\n\n".join(blocks)


USER_TEMPLATE = """仓库上下文：
{contexts}
{web_section}
用户问题：
{query}"""
