"""审查前：请求上下文 → 只读工具取证 → 生成分析报告。"""
import asyncio
import json
from pydantic import ValidationError
from backend.agents.code_review.analysis.state import ContextRequest, ChangeAnalysis
from backend.agents.code_review.analysis.prompts import PLAN_PROMPT, REPORT_PROMPT
from backend.agents.code_review.analysis.tools import RepositoryReader

class AnalysisOutputError(ValueError):
    def __init__(self, reason):
        self.public_message = f'模型输出不符合约束：{reason}（自动纠正一次后仍失败）'
        super().__init__(self.public_message)


def validation_summary(exc):
    """Only field names and constraints, never source text or model output."""
    hints = []
    for error in exc.errors(include_input=False)[:4]:
        field = '.'.join(str(p) for p in error['loc'])
        ctx = error.get('ctx') or {}
        if error['type'] == 'string_too_long':
            reason = f"超过 {ctx.get('max_length')} 字符"
        elif error['type'] == 'too_long':
            reason = f"超过 {ctx.get('max_length')} 项"
        else:
            reason = error['type']
        hints.append(f'{field} {reason}')
    return '；'.join(hints)


async def ask(schema, system, data):
    from langchain_core.messages import HumanMessage, SystemMessage
    from backend.core.llm_factory import get_structured_llm
    messages = [SystemMessage(content=system),
                HumanMessage(content=json.dumps(data, ensure_ascii=False))]
    llm = get_structured_llm('code_analysis', schema)
    for attempt in range(2):
        try:
            response = await asyncio.wait_for(llm.ainvoke(messages), timeout=150)
            return schema.model_validate(response)
        except ValidationError as exc:
            reason = validation_summary(exc)
            if attempt:
                raise AnalysisOutputError(reason) from exc
            # Retry the same evidence with explicit constraints. Do not truncate or accept invalid reports.
            messages.append(HumanMessage(content=(
                f'上一轮输出未通过结构校验：{reason}。请根据相同证据重新生成简短结果，'
                '严格满足字段类型、长度和列表数量限制；不要逐条复述代码或扩写分析。')))

async def analyze_change(root, mode, head_ref, file, contexts, basis, model=ask):
    reader = await asyncio.to_thread(RepositoryReader, root, mode, head_ref)
    evidence, seen, notes = [], set(), []
    async def read(path, start):
        try:
            item = await asyncio.to_thread(reader.read, path, start)
            key = (item['path'], item['start_line'])
            if key not in seen and len(evidence) < 24 and sum(len(e['content']) for e in evidence) + len(item['content']) <= 30000:
                seen.add(key)
                evidence.append(dict(id=f'E{len(evidence) + 1}', **item))
            elif key not in seen:
                notes.append('证据达到 24 个窗口或 30000 字符的上限')
        except (OSError, ValueError) as exc:
            notes.append(f'{path}: {exc}')
    # RAG 仅作为位置线索，正文从当前审查版本重读。
    for context in contexts[:6]:
        await read(context['path'], max(1, context['start_line']))
    if not file.get('deleted_file'):
        start = max(1, (file.get('hunks') or [{'start_line': 1}])[0]['start_line'] - 15)
        await read(file['path'], start)
    if len(reader.paths) > 300:
        notes.append('候选路径清单仅展示前 300 个文件，搜索仍可覆盖扫描限额内的其他文件')
    request = ContextRequest.model_validate(await model(ContextRequest, PLAN_PROMPT, dict(
        diff=file, review_basis=basis, evidence=evidence, candidate_paths=reader.paths[:300], limitations=notes)))
    for item in request.reads:
        await read(item.path, item.start_line)
    for term in request.search_terms:
        try:
            matches = await asyncio.to_thread(reader.search, term)
            for item in matches:
                await read(item['path'], item['start_line'])
        except ValueError as exc:
            notes.append(str(exc))
    notes.extend(reader.notes)
    report = ChangeAnalysis.model_validate(await model(ChangeAnalysis, REPORT_PROMPT, dict(
        diff=file, review_basis=basis, requested_context=request.model_dump(), evidence=evidence, limitations=notes)))
    valid_ids = {e['id'] for e in evidence}
    if any(not set(dep.evidence_ids) <= valid_ids for dep in report.dependencies):
        raise ValueError('分析报告引用不存在的代码证据')
    return dict(**report.model_dump(), evidence=evidence, tool_requests=request.model_dump(),
                limitations=list(dict.fromkeys(notes)))
