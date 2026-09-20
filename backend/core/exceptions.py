# backend/core/exceptions.py
# CodePilot 统一异常体系：所有自定义异常都继承同一个基类，
# 便于「一次捕获全部」以及按「可重试 / 不可重试」分类处理。


class CodePilotBaseError(Exception):
    """所有自定义异常的基类。
    比普通异常多带两样上下文：是哪个 Agent 出的错、以及任意细节字典。"""

    # 是否属于「可重试」类别，重试层（core/retry.py）据此决策
    retryable: bool = False

    def __init__(self, message: str, agent_type: str = "", details: dict = None):
        super().__init__(message)
        self.agent_type = agent_type   # 出错的 Agent（code_qa/code_review/...），方便日志与排障
        self.details = details or {}   # 细节字典；传 None 时用空字典兜底


class LLMAPIError(CodePilotBaseError):
    """大模型 API 调用失败（超时 / 限流 / 网络错误）。【可重试】"""
    retryable = True


class RetryExhaustedError(CodePilotBaseError):
    """三层容错重试耗尽仍失败；原始异常放在 details['last_error']。【终止，不再重试】"""


class AgentExecutionError(CodePilotBaseError):
    """Agent 业务逻辑执行失败。"""


class PipelineError(CodePilotBaseError):
    """多 Agent Pipeline（Orchestrator 串行编排）失败。"""


class IntentRouteError(CodePilotBaseError):
    """意图识别路由失败（没判断出该交给哪个 Agent）。"""


class DatabaseError(CodePilotBaseError):
    """PostgreSQL 连接或操作失败。【可重试】"""
    retryable = True


class MilvusConnectionError(CodePilotBaseError):
    """Milvus 向量库连接失败。【可重试】"""
    retryable = True


class MCPToolError(CodePilotBaseError):
    """MCP 工具调用失败（FileSystem / Git / Shell&Docker / SAST）。【可重试】"""
    retryable = True


class IndexBuildError(CodePilotBaseError):
    """代码索引流水线失败（切片 / 向量化 / 写入 Milvus）。【可重试】"""
    retryable = True


class RepoNotIndexedError(CodePilotBaseError):
    """目标仓库索引未就绪（index_status != indexed），QA 请求被拦截。【不可重试】"""


class FileParseError(CodePilotBaseError):
    """文档解析失败（PDF / Word / PPT，Doc Insight 入口）。"""


class InvalidInputError(CodePilotBaseError):
    """用户输入不合法。【不可重试】（重试也不会变合法）"""


class AuthenticationError(CodePilotBaseError):
    """认证失败（登录 / JWT 校验）。【不可重试】"""


class ReviewApplyConflictError(CodePilotBaseError):
    """code_review HitL 打补丁：suggested_diff 与仓库当前代码冲突（git apply --check 失败）。
    【不可重试】（代码不变重试仍冲突）；API 层映射 409，finding 保持 pending 待人工处理。"""


class ReviewHitlStateError(CodePilotBaseError):
    """code_review HitL 状态非法：finding 的 hitl_status 非 pending 却收到 apply/reject。
    【不可重试】；API 层映射 409。"""
