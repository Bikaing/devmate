# backend/agents/code_qa/prompts.py
# code_qa 提示词：系统人设 + 检索上下文序列化。纯字符串模块，不引 langchain（保持轻量）。

SYSTEM_PROMPT = """你是 DevMate，一名代码仓库问答工程师，只依据提供的仓库上下文回答代码问题。
规则：
1. 代码事实只来自给定上下文，禁止凭记忆编造 API、行号或行为。
2. 上下文不足时：明确说明缺什么、给出验证思路，不要硬答。
3. 涉及代码片段用 Markdown 代码块并标注语言。
4. 回答末尾用 `路径:L起-L止` 格式列出引用行号，符号级命中附符号名。
5. 跟随用户提问的语言作答。"""

NO_CONTEXT_HINT = "(索引中未命中相关代码：请基于通用知识谨慎回答，并明确告知该回答不基于当前仓库。)"

CONTEXT_BLOCK = """--- 上下文 {i}: {path} ({language}) 命中: {cites} ---
{content}"""


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

用户问题：
{query}"""
