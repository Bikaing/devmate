"""code_review 规则预检：不依赖 LLM 的确定性检查，先于模型审查产出 rule 类 findings。

规则清单（与设计对齐）：
1. 密钥正则：扫描新增行，命中私钥/AWS AK/通用密钥赋值/带密码连接串 → critical/security；
2. 单文件超大变更：added+deleted > LARGE_FILE_LINES → warning/style（仍送 LLM 审查）；
3. 删除测试文件：deleted_file 且路径为测试文件 → warning/test；
4. 直改 lockfile / 超宽跳过：来自 diff_stats.skipped 的 lockfile、变更过宽条目 → info/style。

rule 类 finding 一律 suggested_diff=None（规则不给补丁，只提示），source='rule'。
行号口径与 diff_parser 一致：锚定 head 版本（新文件侧）；无行号归属的规则取 1。
"""
from __future__ import annotations

import re

from backend.core.logger import get_logger

logger = get_logger(__name__)

# 单文件变更行数告警阈值（超过则提示拆分；>2000 的已被 diff_parser 跳过审查）
LARGE_FILE_LINES = 500

# 密钥模式：(名称, 正则)，扫描对象为 hunk 新增行（去掉行首 '+' 后的内容）
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("私钥块", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("通用密钥赋值", re.compile(
        r"(?i)\b(?:api[_-]?key|apikey|secret|token|passwd|password)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
    ("带密码的连接串", re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|redis|mongodb(?:\+srv)?|amqp)://[^:/\s]+:[^@/\s]+@")),
]

# 测试文件判定：tests/|test/ 目录，或 test_x.* / x_test.py / x.test.js / x.spec.ts 等命名
_TEST_DIR_RE = re.compile(r"(^|/)tests?/")
_TEST_FILE_RE = re.compile(r"(?:^test_.*|.*_test\.py$|.*\.(?:test|spec)\.[A-Za-z0-9]+$)")


def _is_test_path(path: str) -> bool:
    """判断仓库相对路径是否为测试文件。"""
    if _TEST_DIR_RE.search(path):
        return True
    return bool(_TEST_FILE_RE.match(path.rsplit("/", 1)[-1]))


def _iter_added_lines(hunks: list[dict]):
    """yield (新侧行号, 行内容含行首'+')：仅遍历新增行，行号按 hunk 新侧起点推算。"""
    for h in hunks:
        line_no = int(h["start_line"])
        for raw in (h.get("text") or "").splitlines():
            if raw.startswith("@@") or raw.startswith("\\"):
                continue  # hunk 头 / "\ No newline" 标记不占行号
            if raw.startswith("-"):
                continue  # 删除行不占新侧行号
            if raw.startswith("+"):
                yield line_no, raw
            line_no += 1


def _finding(file: str, start_line: int, end_line: int, severity: str,
             category: str, title: str, suggestion: str) -> dict:
    """rule 类 finding 统一构造（suggested_diff 恒为 None）。"""
    return {
        "file": file,
        "start_line": start_line,
        "end_line": end_line,
        "severity": severity,
        "category": category,
        "title": title,
        "suggestion": suggestion,
        "suggested_diff": None,
        "source": "rule",
    }


def check_rules(diff_files: list[dict], diff_stats: dict) -> list[dict]:
    """对 diff_parser 产物跑确定性规则，返回 rule 类 findings 列表（纯函数，无 IO）。"""
    findings: list[dict] = []

    for f in diff_files:
        path = f["path"]
        # 规则 1：新增行密钥扫描（同一行只报首个命中的模式）
        for line_no, raw in _iter_added_lines(f.get("hunks") or []):
            content = raw[1:]
            for name, pattern in _SECRET_PATTERNS:
                if pattern.search(content):
                    findings.append(_finding(
                        path, line_no, line_no, "critical", "security",
                        f"疑似硬编码密钥（{name}）",
                        "将该凭据移至环境变量或密钥管理服务，并视已泄露凭据为失效、尽快轮换。"))
                    break
        # 规则 2：单文件超大变更
        total = int(f.get("added", 0)) + int(f.get("deleted", 0))
        if total > LARGE_FILE_LINES:
            findings.append(_finding(
                path, 1, 1, "warning", "style",
                f"单文件变更过大（+{f.get('added', 0)}/-{f.get('deleted', 0)}）",
                "建议拆分为多个逻辑独立的提交，便于审查与回滚。"))
        # 规则 3：删除测试文件
        if f.get("deleted_file") and _is_test_path(path):
            findings.append(_finding(
                path, 1, 1, "warning", "test",
                "删除了测试文件",
                "确认删除意图：若功能仍保留，应同步迁移或补充对应测试覆盖。"))

    # 规则 4：被跳过的 lockfile 直改 / 超宽文件提示（来自 diff_stats.skipped）
    for item in (diff_stats or {}).get("skipped") or []:
        reason = item.get("reason", "")
        path = item.get("path", "")
        if reason == "lockfile":
            findings.append(_finding(
                path, 1, 1, "info", "style",
                "lockfile 被直接修改",
                "lockfile 应由包管理器生成；确认手工改动的必要性及其与依赖声明的一致性。"))
        elif reason.startswith("变更过宽"):
            findings.append(_finding(
                path, 1, 1, "info", "style",
                "变更过宽已跳过自动审查",
                "该文件变更宽度超过阈值，未纳入 LLM 审查；建议拆分提交或人工审查。"))

    logger.info("code_review.rules_checked",
                files=len(diff_files), findings=len(findings))
    return findings
