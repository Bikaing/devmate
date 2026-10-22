"""审查前分析的工具请求与证据交接契约。"""
from pydantic import BaseModel, Field

class ReadRequest(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    start_line: int = Field(ge=1, le=100000)

class ContextRequest(BaseModel):
    missing_context: str = Field(max_length=1500)
    search_terms: list[str] = Field(max_length=3, description='搜索的符号或字面文本，不是正则或命令')
    reads: list[ReadRequest] = Field(max_length=3)

class Dependency(BaseModel):
    relationship: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)

class ChangeAnalysis(BaseModel):
    changed_behavior: str = Field(min_length=1, max_length=2000,
        description='简短概括主要行为变化，建议 100～500 字符，硬上限 2000 字符；不逐条复述代码')
    dependencies: list[Dependency] = Field(max_length=8)
    review_focus: list[str] = Field(min_length=1, max_length=8)
    unknowns: list[str] = Field(max_length=8)
