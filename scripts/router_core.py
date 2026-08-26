#!/usr/bin/env python3
"""Pure-stdlib state, classification, telemetry, and receipt helpers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from provider_policy import (
    EXECUTION_PROFILES,
    LIGHT_PROFILES,
    LIGHT_PROFILE_LOCAL_TEXT_FIRST,
    LIGHT_PROFILE_LUNA_STABLE,
    PROFILE_GLM_FIRST,
    PROFILE_STABLE,
)

PLUGIN_ID = "codex-smart-router"
MODES = {"OFF", "SHADOW", "ON"}
ECONOMICS_POLICIES = {"V1_COMPAT", "V2_STATIC"}
DEFAULT_ECONOMICS_POLICY = "V2_STATIC"
ROLES = {
    "router_scout",
    "router_worker",
    "router_reviewer",
    "router_monitor",
    "router_tester",
    "router_docs",
}
MODEL_ROLES = ROLES - {"router_monitor"}
WRITER_ROLES = {"router_worker", "router_tester", "router_docs"}
ROLE_LABELS = {
    "router_scout": "Luna · 只读侦察",
    "router_worker": "Terra · 执行",
    "router_reviewer": "Terra · 审查",
    "router_monitor": "确定性长等待（无模型）",
    "router_tester": "Luna · 测试",
    "router_docs": "Luna · 文档",
}
STATE_TTL_SECONDS = 30 * 24 * 60 * 60

NEGATION_PREFIX = re.compile(
    r"(?:不要|不得|禁止|避免|无需|无须|不用|不需要|请勿|do\s+not|don't|never|without)"
    r"[\s\w\u4e00-\u9fff、/+-]{0,14}$",
    re.I,
)
EXPLICIT_READ_ONLY = (
    "只读",
    "不得写入",
    "不要写入",
    "禁止写入",
    "不修改任何内容",
    "不得修改任何内容",
    "不要修改任何内容",
    "read-only",
    "read only",
    "do not write",
    "do not modify any",
    "no file changes",
)
GLOBAL_READ_ONLY_PATTERNS = (
    re.compile(r"(?:^|[。；;，,：:!?！？])\s*(?:只(?:能|可)?|仅(?:能|可)?)\s*(?:查看|阅读|分析)", re.I),
    re.compile(r"(?:^|[。；;，,：:!?！？])\s*(?:代码|文件|工作区)?\s*(?:不可|不得|不要|禁止|请勿)写入", re.I),
    re.compile(r"(?:^|[。；;，,：:!?！？])\s*(?:不做|不要做|不得做|请勿做)\s*任何(?:代码|文件)?(?:改动|修改|变更)", re.I),
    re.compile(r"(?:^|[。；;，,：:!?！？])\s*(?:不要|不得|不可|禁止|请勿)(?:改动|修改|编辑|变更)(?:任何|所有|任意)?(?:代码|文件|内容)", re.I),
    re.compile(r"(?:^|[.;,!?:])\s*(?:do\s+not|don't|never)\s+(?:edit|modify|change|write\s+to)\b", re.I),
    re.compile(r"(?:^|[.;,!?:])\s*(?:no\s+edits?|no\s+(?:file\s+)?changes|without\s+(?:editing|changes))\b", re.I),
)
PLANNING_ONLY_PATTERNS = (
    re.compile(r"如何.{0,6}实现", re.I),
    re.compile(r"实现(?:方案|思路|建议)", re.I),
    re.compile(r"修复(?:方案|思路|建议)", re.I),
    re.compile(r"\bhow\s+to\s+implement\b", re.I),
    re.compile(r"\bimplementation\s+plan\b", re.I),
    re.compile(r"\b(?:propose|suggest|recommend)\s+(?:a\s+)?fix\b", re.I),
)
POST_WRITE_READ_ONLY_VERIFICATION = re.compile(
    r"(?:完成后|随后|然后|最后|afterwards|then)\s*(?:再|仅)?\s*"
    r"(?:进行)?\s*(?:只读|read[- ]only)\s*(?:地)?\s*"
    r"(?:验证|检查|核对|verify|check)",
    re.I,
)
ROUTING_AUTHORIZATION_METADATA = re.compile(
    r"\bwrite_authorized\s*=\s*true\b|"
    r"用户(?:已)?明确授权执行(?:本次|这一项|该项|此项)?",
    re.I,
)
SECURITY_AUTHORIZATION_ACTION = re.compile(
    r"(?:实现|修复|修改|新增|设计|审查|检查).{0,12}授权"
    r"|授权.{0,8}(?:功能|逻辑|机制|系统|检查|控制|漏洞)",
    re.I,
)

CONTROL_PATTERNS = (
    (re.compile(r"\$router-control\s*(?:经济策略\s*)?(?:v2|保守|static)\b", re.I), "ECON_V2"),
    (re.compile(r"/router\s+policy\s+v2\b", re.I), "ECON_V2"),
    (re.compile(r"\$router-control\s*(?:经济策略\s*)?(?:v1|兼容|compat)\b", re.I), "ECON_V1"),
    (re.compile(r"/router\s+policy\s+v1\b", re.I), "ECON_V1"),
    (re.compile(r"\$router-control\s*(?:local\s*(?:开启|启用|on)|(?:开启|启用)\s*local)\b", re.I), "LOCAL_ON"),
    (re.compile(r"/router\s+local\s+on\b", re.I), "LOCAL_ON"),
    (re.compile(r"\$router-control\s*(?:local\s*(?:关闭|停用|off)|(?:关闭|停用)\s*local)\b", re.I), "LOCAL_OFF"),
    (re.compile(r"/router\s+local\s+off\b", re.I), "LOCAL_OFF"),
    (re.compile(r"\$router-control\s*(?:glm\s*(?:开启|启用|on)|(?:开启|启用)\s*glm)\b", re.I), "GLM_ON"),
    (re.compile(r"/router\s+glm\s+on\b", re.I), "GLM_ON"),
    (re.compile(r"\$router-control\s*(?:glm\s*(?:关闭|停用|off)|(?:关闭|停用)\s*glm)\b", re.I), "GLM_OFF"),
    (re.compile(r"/router\s+glm\s+off\b", re.I), "GLM_OFF"),
    (re.compile(r"(?:\$router-control\s*)?(?:开启|启用)(?:智能)?路由", re.I), "ON"),
    (re.compile(r"\$router-control\s*(?:开启|启用|on)\b", re.I), "ON"),
    (re.compile(r"/router\s+on\b", re.I), "ON"),
    (re.compile(r"(?:\$router-control\s*)?(?:影子模式|影子|shadow)\b", re.I), "SHADOW"),
    (re.compile(r"/router\s+shadow\b", re.I), "SHADOW"),
    (re.compile(r"(?:\$router-control\s*)?(?:关闭|停用)(?:智能)?路由", re.I), "OFF"),
    (re.compile(r"\$router-control\s*(?:关闭|停用|off)\b", re.I), "OFF"),
    (re.compile(r"/router\s+off\b", re.I), "OFF"),
    (re.compile(r"\$router-control\s*(?:状态|status)\b", re.I), "STATUS"),
    (re.compile(r"/router\s+status\b", re.I), "STATUS"),
    (re.compile(r"\$router-control(?:\s*(?:帮助|help))?\s*$", re.I), "HELP"),
    (re.compile(r"/router\s+help\b", re.I), "HELP"),
)

HIGH_RISK = {
    "security": (
        "安全漏洞",
        "鉴权",
        "认证",
        "权限",
        "oauth",
        "auth",
        "authentication",
        "authorization",
        "permission",
        "csrf",
        "xss",
        "注入",
    ),
    "secrets": ("密钥", "secret", "token", "密码", "credential", "证书", "private key"),
    "production": ("生产环境", "线上", "发布", "部署", "production", "prod", "release", "rollout"),
    "data": ("数据库迁移", "schema migration", "删库", "清空数据", "drop table", "truncate", "数据迁移"),
    "destructive": ("rm -rf", "永久删除", "不可逆", "销毁", "wipe", "destroy", "force push"),
    "money": ("支付", "扣款", "账单", "payment", "billing", "转账"),
    "architecture": ("系统架构", "跨系统", "架构决策", "architecture", "distributed transaction"),
    "concurrency": ("并发", "竞态", "死锁", "race condition", "deadlock", "事务隔离", "并发一致性", "事务一致性"),
}

CATEGORY_TERMS = {
    "router_monitor": ("等待", "轮询", "监控", "状态检查", "watch", "poll", "monitor", "babysit"),
    "router_reviewer": ("代码审查", "跨文件复核", "合同一致性", "一致性检查", "缺陷归因", "review", "找风险", "审阅", "audit"),
    "router_tester": ("测试", "用例", "test", "pytest", "unittest", "验证失败", "跑一下测试"),
    "router_docs": ("文档", "readme", "说明书", "注释更新", "documentation", "changelog"),
    "router_scout": (
        "搜索", "查找", "盘点", "批量核查", "轨迹审阅", "manifest", "sha", "调研代码", "读日志",
        "日志分析", "分析所有日志", "检查日志", "日志文件", "日志归类", "身份盘点", "定位文件", "收集证据",
        "扫描", "scan", "search", "inventory", "inspect logs", "analyze logs", "scan repository",
    ),
    "router_worker": (
        "实现",
        "修复",
        "改代码",
        "重构",
        "新增功能",
        "创建",
        "新建",
        "写入",
        "bug",
        "implement",
        "fix",
        "refactor",
        "patch",
        "create",
        "write",
    ),
}

BATCH_TERMS = (
    "批量", "全部", "所有", "多个", "整仓", "全仓", "仓库", "目录", "测试套件", "轨迹", "manifest",
    "案例", "样本", "batch", "all files", "repository", "workspace", "test suite", "multiple",
)
PATH_SIGNAL = re.compile(r"(?:^|\s)(?:\.?\.?/|/)[^\s，。；;]+|\b[\w.-]+/(?:[\w./-]+)")
COUNT_SIGNAL = re.compile(r"(?<!\d)(?:[3-9]|[1-9]\d+)\s*(?:个|项|份|组|files?|modules?|cases?)", re.I)
COMPLEX_REVIEW_TERMS = ("跨文件", "跨模块", "合同一致性", "缺陷归因", "cross-file", "cross module", "contract")

STRONG_BATCH_TERMS = (
    "批量",
    "所有日志",
    "全部日志",
    "所有文件",
    "全部文件",
    "整仓",
    "全仓",
    "整个仓库",
    "整个目录",
    "测试套件",
    "batch",
    "all files",
    "entire repository",
    "whole repository",
    "test suite",
)
SINGLE_SCOPE_TERMS = (
    "单个文件",
    "一个文件",
    "这个文件",
    "当前文件",
    "单文件",
    "single file",
    "this file",
)
FILE_SIGNAL = re.compile(
    r"(?<![\w.-])(?:README|CHANGELOG|LICENSE)(?:\.[\w.-]+)?\b|"
    r"(?<![\w.-])[\w.-]+\.(?:py|js|jsx|ts|tsx|json|toml|ya?ml|md|txt|log|html|css|sql)\b",
    re.I,
)
COUNT_VALUE_SIGNAL = re.compile(
    r"(?<!\d)(\d{1,3})\s*(?:个|项|份|组|files?\b|modules?\b|cases?\b|tests?\b|logs?\b)",
    re.I,
)
CHINESE_COUNT_TERMS = {"一个": 1, "单个": 1, "两个": 2, "三个": 3, "四个": 4, "五个": 5, "六个": 6, "七个": 7, "八个": 8, "九个": 9}

DETERMINISTIC_TOOL_PATTERNS = (
    (
        "path_exists",
        re.compile(
            r"(?:文件|路径|目录).{0,100}(?:是否存在|存在吗)|"
            r"(?:[\w./-]+\.[\w.-]+)[^\u3002；;\n]{0,80}(?:是否存在|存在吗)|"
            r"does\s+(?:this\s+)?(?:file|path|directory)\s+exist",
            re.I,
        ),
    ),
    (
        "exact_search",
        re.compile(r"(?:精确搜索|查找字符串|搜索键名|查找键名|查找引用|\brg\b|exact\s+search)", re.I),
    ),
    (
        "metadata",
        re.compile(r"(?:统计文件数|文件数量|文件个数|行数|sha-?256|哈希|hash|exif|图片尺寸|图像尺寸|页面数|页数)", re.I),
    ),
    (
        "git_status",
        re.compile(r"(?:git\s+(?:status|diff\s+--stat|log)|查看\s*git\s*状态|diff\s*统计)", re.I),
    ),
    (
        "schema_validation",
        re.compile(r"(?:校验|验证|validate).{0,24}(?:json|ya?ml|toml|schema)", re.I),
    ),
    (
        "test_command",
        re.compile(
            r"(?:运行|执行|跑一下|run)[^\u3002；;\n]{0,160}"
            r"(?:现有)?(?:测试|pytest|unittest|npm\s+test|pnpm\s+test|yarn\s+test|lint|typecheck|build)",
            re.I,
        ),
    ),
)
SEMANTIC_TOOL_BLOCKERS = (
    "分析失败",
    "缺陷归因",
    "解释原因",
    "总结原因",
    "修复",
    "实现",
    "修改",
    "重构",
    "评估质量",
    "提出建议",
    "root cause",
    "explain why",
    "fix",
    "implement",
    "refactor",
)
MULTIMODAL_METADATA_TERMS = ("图片尺寸", "图像尺寸", "exif", "图片格式", "页面数", "页数", "哈希", "hash")
MULTIMODAL_SEMANTIC_TERMS = (
    "图片内容",
    "图像内容",
    "截图内容",
    "识别图片",
    "分析图片",
    "分析截图",
    "视觉分析",
    "ppt 页面内容",
    "幻灯片内容",
    "image content",
    "analyze image",
    "screenshot content",
    "visual review",
)
DESTRUCTIVE_FILE_TARGET = (
    r"(?:图片|图像|截图|文件|目录|路径|"
    r"images?|pictures?|screenshots?|files?|director(?:y|ies)|paths?|assets?|"
    r"png|jpe?g|gif|webp|svg|pdf|"
    r"[\w.-]+/[\w./-]+)"
)
DESTRUCTIVE_FILE_ACTION_PATTERNS = (
    re.compile(
        rf"(?:删除|删掉|移除|清理|delete|remove)[^。；;\n]{{0,64}}{DESTRUCTIVE_FILE_TARGET}",
        re.I,
    ),
    re.compile(
        rf"{DESTRUCTIVE_FILE_TARGET}[^。；;\n]{{0,64}}(?:删除|删掉|移除|清理|delete|remove)",
        re.I,
    ),
)
DESTRUCTIVE_ACTION_TERM = re.compile(r"删除|删掉|移除|清理|delete|remove", re.I)

ROLE_MIN_ITEMS = {
    "router_scout": 4,
    "router_reviewer": 4,
    "router_worker": 4,
    "router_tester": 4,
    "router_docs": 4,
}
ROLE_PARENT_REVIEW_CAP = {
    "router_scout": 0.30,
    "router_reviewer": 0.35,
    "router_worker": 0.35,
    "router_tester": 0.30,
    "router_docs": 0.30,
    "router_monitor": 0.0,
}

WRITE_INTENT_PATTERNS = {
    "router_worker": (
        re.compile(r"实现|修复|改代码|重构|新增功能|\b(?:implement|fix|refactor|patch)\b", re.I),
        re.compile(r"创建|新建|写入|\b(?:create|write)\b", re.I),
        re.compile(r"(?:创建|新建|新增|修改|更新).{0,10}(?:文件|模块|功能|代码|脚本)", re.I),
    ),
    "router_tester": (
        re.compile(r"(?:补充|新增|添加|编写|写|完善|修复).{0,10}(?:测试|用例)", re.I),
        re.compile(
            r"(?:运行|执行|跑).{0,20}(?:测试|用例|pytest|unittest|\b(?:npm|pnpm|yarn)\s+test\b)",
            re.I,
        ),
        re.compile(r"\b(?:add|write|update|fix|run)\b.{0,16}\b(?:tests?|pytest|unittest)\b", re.I),
    ),
    "router_docs": (
        re.compile(r"(?:更新|修改|补充|新增|添加|编写|写|完善).{0,10}(?:readme|文档|说明|changelog)", re.I),
        re.compile(r"\b(?:update|write|edit|add)\b.{0,16}\b(?:readme|docs?|documentation|changelog)\b", re.I),
    ),
}


def data_root(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    plugin_data = os.environ.get("PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data).expanduser().resolve()
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return (base / "plugin-data" / PLUGIN_ID).resolve()


def writer_lock_path(workspace: str | Path) -> Path:
    """Return the cross-process writer lock for one resolved workspace."""
    configured = os.environ.get("CODEX_HOME")
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    resolved = str(Path(workspace).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8", "replace")).hexdigest()
    return codex_home / "smart-router" / "locks" / f"{digest}.lock"


@contextmanager
def workspace_writer_lock(workspace: str | Path) -> Iterator[Path]:
    """Hold a non-blocking OS lock while a writable child is alive."""
    path = writer_lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another writable routing task is active for this workspace") from exc
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def writer_lock_held(workspace: str | Path) -> bool:
    """Check a writer lock without trusting a possibly stale lease file."""
    path = writer_lock_path(workspace)
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()


def state_path(root: Path, session_id: str) -> Path:
    return root / "sessions" / f"{session_key(session_id)}.json"


@contextmanager
def state_key_lock(root: Path, key: str) -> Iterator[None]:
    """Serialize state transitions for one hashed session key across hook processes."""
    lock_dir = root / "state-locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = lock_dir / f"{key}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def session_state_lock(root: Path, session_id: str) -> Iterator[None]:
    with state_key_lock(root, session_key(session_id)):
        yield


def delegation_task_digest(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Bind a runtime lease to exactly the task or wait condition approved by the hook."""
    if "wait_for_condition" in tool_name:
        payload = {
            name: tool_input.get(name)
            for name in ("condition", "target", "expected", "timeout_seconds", "interval_seconds")
            if name in tool_input
        }
    else:
        payload = {"task": str(tool_input.get("task") or "")}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()


def consume_runtime_lease(
    root: Path,
    decision_id: str,
    lease_id: str,
    role: str,
    task_digest: str,
) -> bool:
    """Atomically consume the hook-created lease before MCP starts any work."""
    sessions = root / "sessions"
    if not sessions.is_dir():
        return False
    for path in sessions.glob("*.json"):
        key = path.stem
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            continue
        with state_key_lock(root, key):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            delegation = state.get("current_delegation")
            if not isinstance(delegation, dict):
                continue
            if not (
                state.get("mode") == "ON"
                and delegation.get("decision_id") == decision_id
                and delegation.get("lease_id") == lease_id
                and delegation.get("role") == role
                and delegation.get("task_digest") == task_digest
                and delegation.get("status") == "started"
            ):
                continue
            delegation["status"] = "running"
            delegation["runtime_started_at"] = int(time.time())
            state["current_delegation"] = delegation
            state["updated_at"] = int(time.time())
            _atomic_json(path, state)
            return True
    return False


def default_state(session_id: str) -> dict[str, Any]:
    now = int(time.time())
    return {
        "schema_version": 7,
        "session_key": session_key(session_id),
        "mode": "OFF",
        "execution_profile": PROFILE_STABLE,
        "light_profile": LIGHT_PROFILE_LUNA_STABLE,
        "economics_policy": DEFAULT_ECONOMICS_POLICY,
        "created_at": now,
        "updated_at": now,
        "last_decision": None,
        "repair_attempts": 0,
        "active_writer": None,
        "execution_counts": {"completed": 0, "failed": 0},
        "last_execution": None,
        "recent_execution_keys": [],
        "current_delegation": None,
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def load_state(root: Path, session_id: str) -> dict[str, Any]:
    path = state_path(root, session_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_state(session_id)
    if state.get("mode") not in MODES or state.get("session_key") != session_key(session_id):
        return default_state(session_id)
    # Additive migration keeps an existing session's mode while enabling newer
    # user-facing execution history.
    state["schema_version"] = 7
    profile = str(state.get("execution_profile") or PROFILE_STABLE).upper()
    state["execution_profile"] = profile if profile in EXECUTION_PROFILES else PROFILE_STABLE
    light_profile = str(state.get("light_profile") or LIGHT_PROFILE_LUNA_STABLE).upper()
    state["light_profile"] = light_profile if light_profile in LIGHT_PROFILES else LIGHT_PROFILE_LUNA_STABLE
    economics_policy = str(state.get("economics_policy") or DEFAULT_ECONOMICS_POLICY).upper()
    state["economics_policy"] = (
        economics_policy if economics_policy in ECONOMICS_POLICIES else DEFAULT_ECONOMICS_POLICY
    )
    counts = state.get("execution_counts")
    if not isinstance(counts, dict):
        counts = {}
    def safe_count(name: str) -> int:
        try:
            return max(0, int(counts.get(name, 0)))
        except (TypeError, ValueError):
            return 0

    state["execution_counts"] = {
        "completed": safe_count("completed"),
        "failed": safe_count("failed"),
    }
    if not isinstance(state.get("last_execution"), dict):
        state["last_execution"] = None
    recent = state.get("recent_execution_keys")
    if not isinstance(recent, list) or not all(isinstance(item, str) for item in recent):
        recent = []
    last_execution = state.get("last_execution")
    if not recent and isinstance(last_execution, dict):
        role = last_execution.get("role")
        tool_use_id = last_execution.get("tool_use_id")
        if isinstance(role, str) and isinstance(tool_use_id, str) and role and tool_use_id:
            recent = [f"{role}:{tool_use_id}"]
    state["recent_execution_keys"] = recent[-128:]
    delegation = state.get("current_delegation")
    if not isinstance(delegation, dict):
        delegation = None
    state["current_delegation"] = delegation
    return state


def save_state(root: Path, session_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = int(time.time())
    _atomic_json(state_path(root, session_id), state)


def set_mode(root: Path, session_id: str, mode: str) -> dict[str, Any]:
    mode = mode.upper()
    if mode not in MODES:
        raise ValueError(f"unsupported mode: {mode}")
    state = load_state(root, session_id)
    state["mode"] = mode
    state["last_decision"] = None
    state["repair_attempts"] = 0
    # A mode change affects future routing only. Preserve any writer already in
    # flight so status and its eventual PostToolUse remain lifecycle-accurate.
    save_state(root, session_id, state)
    return state


def set_execution_profile(
    root: Path,
    session_id: str,
    profile: str,
    *,
    activate: bool = False,
) -> dict[str, Any]:
    normalized = profile.upper()
    if normalized not in EXECUTION_PROFILES:
        raise ValueError(f"unsupported execution profile: {profile}")
    state = load_state(root, session_id)
    state["execution_profile"] = normalized
    if activate:
        state["mode"] = "ON"
    state["last_decision"] = None
    state["repair_attempts"] = 0
    save_state(root, session_id, state)
    return state


def set_light_profile(
    root: Path,
    session_id: str,
    profile: str,
    *,
    activate: bool = False,
) -> dict[str, Any]:
    normalized = profile.upper()
    if normalized not in LIGHT_PROFILES:
        raise ValueError(f"unsupported light profile: {profile}")
    state = load_state(root, session_id)
    state["light_profile"] = normalized
    if activate:
        state["mode"] = "ON"
    state["last_decision"] = None
    state["repair_attempts"] = 0
    save_state(root, session_id, state)
    return state


def set_economics_policy(root: Path, session_id: str, policy: str) -> dict[str, Any]:
    normalized = policy.upper()
    if normalized not in ECONOMICS_POLICIES:
        raise ValueError(f"unsupported economics policy: {policy}")
    state = load_state(root, session_id)
    state["economics_policy"] = normalized
    state["last_decision"] = None
    state["repair_attempts"] = 0
    save_state(root, session_id, state)
    return state


def parse_control(prompt: str) -> str | None:
    normalized = " ".join(prompt.strip().split()).rstrip("。.!！")
    for pattern, action in CONTROL_PATTERNS:
        if pattern.fullmatch(normalized):
            return action
    return None


def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    if term.isascii() and re.search(r"[a-z0-9]", term, re.I):
        return re.compile(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])", re.I)
    return re.compile(escaped)


def _matches(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if _term_pattern(term).search(text)]


def _is_negated(text: str, start: int) -> bool:
    """Return whether a nearby same-clause prefix negates the match at start."""
    prefix = text[max(0, start - 28) : start]
    clause = re.split(r"[。；;.!?！？:\n]", prefix)[-1]
    return bool(NEGATION_PREFIX.search(clause))


def _positive_matches(text: str, terms: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for term in terms:
        if any(not _is_negated(text, match.start()) for match in _term_pattern(term).finditer(text)):
            found.append(term)
    return found


def _write_intent_matches(text: str, role: str) -> list[str]:
    matches: list[str] = []
    planning_spans = [match.span() for pattern in PLANNING_ONLY_PATTERNS for match in pattern.finditer(text)]
    for pattern in WRITE_INTENT_PATTERNS.get(role, ()):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in planning_spans):
                continue
            if not _is_negated(text, match.start()):
                matches.append(match.group(0))
    return matches


def _explicit_read_only(text: str) -> bool:
    # A bounded writer may be asked to perform read-only verification after
    # completing the authorized write. That clause must not turn the whole task
    # into read-only; all other explicit read-only markers remain fail-closed.
    scoped = POST_WRITE_READ_ONLY_VERIFICATION.sub("", text)
    return any(term in scoped for term in EXPLICIT_READ_ONLY) or any(
        pattern.search(scoped) for pattern in GLOBAL_READ_ONLY_PATTERNS
    )


def _risk_reasons(text: str) -> list[str]:
    scoped = ROUTING_AUTHORIZATION_METADATA.sub("", text)
    reasons = [f"high_risk:{code}" for code, terms in HIGH_RISK.items() if _matches(scoped, terms)]
    destructive_action = False
    for pattern in DESTRUCTIVE_FILE_ACTION_PATTERNS:
        for match in pattern.finditer(scoped):
            action = DESTRUCTIVE_ACTION_TERM.search(match.group(0))
            if action and not _is_negated(scoped, match.start() + action.start()):
                destructive_action = True
                break
        if destructive_action:
            break
    if destructive_action and "high_risk:destructive" not in reasons:
        reasons.append("high_risk:destructive")
    if SECURITY_AUTHORIZATION_ACTION.search(scoped) and "high_risk:security" not in reasons:
        reasons.append("high_risk:security")
    return reasons


def write_authorized_for(prompt: str, role: str) -> bool:
    """Require a positive, non-negated write action for every writable role."""
    if role not in WRITER_ROLES:
        return False
    text = " ".join(prompt.lower().split())
    role_matches = _write_intent_matches(text, role)
    # Terra's writable multimodal lane is router_worker. A user may explicitly
    # request a docs/test change based on image semantics; preserve that positive
    # write authorization when remapping away from text-only Luna roles.
    if role == "router_worker" and any(term in text for term in MULTIMODAL_SEMANTIC_TERMS):
        role_matches.extend(_write_intent_matches(text, "router_docs"))
        role_matches.extend(_write_intent_matches(text, "router_tester"))
    return (
        not _risk_reasons(text)
        and not _explicit_read_only(text)
        and bool(role_matches)
    )


def _deterministic_tool_kind(text: str) -> str | None:
    if any(term in text for term in SEMANTIC_TOOL_BLOCKERS):
        return None
    for kind, pattern in DETERMINISTIC_TOOL_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def _economic_features(text: str, role: str, found: list[str]) -> dict[str, Any]:
    batch_hits = _matches(text, BATCH_TERMS)
    strong_batch_hits = _matches(text, STRONG_BATCH_TERMS)
    legacy_path_hits = PATH_SIGNAL.findall(text)
    legacy_count_hits = COUNT_SIGNAL.findall(text)
    path_matches = list(PATH_SIGNAL.finditer(text))
    paths = {match.group(0).strip() for match in path_matches}
    for file_match in FILE_SIGNAL.finditer(text):
        # A path regex already owns its basename. Counting the nested filename
        # again made four explicit paths look like eight independent items.
        if any(
            path_match.start() <= file_match.start() and file_match.end() <= path_match.end()
            for path_match in path_matches
        ):
            continue
        paths.add(file_match.group(0).strip())
    numeric_counts = [int(match.group(1)) for match in COUNT_VALUE_SIGNAL.finditer(text)]
    chinese_counts = [value for term, value in CHINESE_COUNT_TERMS.items() if term in text]
    explicit_item_count = max([0, *numeric_counts, *chinese_counts])
    complex_review_hits = _matches(text, COMPLEX_REVIEW_TERMS) if role == "router_reviewer" else []
    broad_floor = 0
    if strong_batch_hits:
        broad_floor = 4
        if any(
            marker in text
            for marker in (
                "所有日志",
                "全部日志",
                "所有文件",
                "全部文件",
                "整仓",
                "全仓",
                "整个仓库",
                "entire repository",
                "whole repository",
                "all files",
            )
        ):
            broad_floor = 8
    estimated_items = max(len(paths), explicit_item_count, broad_floor)
    deterministic_kind = _deterministic_tool_kind(text)
    semantic_multimodal = any(term in text for term in MULTIMODAL_SEMANTIC_TERMS)
    single_scope = any(term in text for term in SINGLE_SCOPE_TERMS) or (
        len(paths) == 1 and explicit_item_count <= 1 and not strong_batch_hits
    )
    micro_task = bool(deterministic_kind) or (
        (single_scope or estimated_items <= 1)
        and not strong_batch_hits
        and not complex_review_hits
        and not semantic_multimodal
    )
    writer = role in WRITER_ROLES
    independent_bounded_package = bool(
        role == "router_monitor"
        or semantic_multimodal
        or len(paths) >= 2
        or explicit_item_count >= 2
        or (strong_batch_hits and not writer)
        or (complex_review_hits and estimated_items >= 2)
    )
    if deterministic_kind or micro_task:
        sol_turn_bucket = "1-3"
    elif estimated_items >= 4 or semantic_multimodal:
        sol_turn_bucket = "4+"
    else:
        sol_turn_bucket = "unknown"
    legacy_work_units = min(
        12,
        1
        + len(found)
        + min(4, len(batch_hits) * 2)
        + min(3, len(legacy_path_hits))
        + min(3, len(legacy_count_hits) * 2)
        + min(2, len(complex_review_hits) * 2)
        + (1 if len(text) >= 160 else 0),
    )
    return {
        "candidate_role": role,
        "deterministic_tool_possible": bool(deterministic_kind),
        "deterministic_tool_kind": deterministic_kind,
        "semantic_multimodal": semantic_multimodal,
        "single_scope": single_scope,
        "micro_task": micro_task,
        "strong_batch": bool(strong_batch_hits),
        "strong_batch_signals": strong_batch_hits[:4],
        "unique_path_count": len(paths),
        "explicit_item_count": explicit_item_count,
        "independent_item_count_estimate": estimated_items,
        "estimated_sol_turns": sol_turn_bucket,
        "independent_bounded_package": independent_bounded_package,
        "coalesce_candidate": (
            role in {"router_scout", "router_reviewer"} and 4 <= estimated_items <= 12
        ),
        "work_units_legacy": legacy_work_units,
    }


def _inline_candidate(
    role: str,
    features: dict[str, Any],
    *reason_codes: str,
    confidence: float = 0.94,
) -> dict[str, Any]:
    return {
        "decision": "INLINE_SOL",
        "role": None,
        "risk": "LOW",
        "confidence": confidence,
        "reason_codes": [*reason_codes, f"candidate:{role}"],
        "write_authorized": False,
        "estimated_work_units": features["work_units_legacy"],
        "estimated_parent_review_ratio": 1.0,
        "task_bucket": "micro_query" if features["micro_task"] else "uncertain_scope",
        "gate_features": features,
        "cost_estimate_status": "cold_start_static_proxy",
    }


def _v1_economic_decision(
    role: str,
    found: list[str],
    reasons: list[str],
    write_authorized: bool,
    features: dict[str, Any],
    count: int,
) -> dict[str, Any]:
    work_units = int(features["work_units_legacy"])
    if role != "router_monitor" and work_units < 4:
        return _inline_candidate(role, features, "routing_overhead", f"work_units:{work_units}", confidence=0.9)
    expected_review_ratio = round(min(0.3, 1.0 / max(4, work_units)), 2)
    reasons.extend([f"work_units:{work_units}", f"review_ratio:{expected_review_ratio:.2f}", "policy:v1_compat"])
    return {
        "decision": "DELEGATE",
        "role": role,
        "risk": "LOW",
        "confidence": min(0.96, 0.78 + 0.06 * count),
        "reason_codes": reasons,
        "write_authorized": write_authorized,
        "estimated_work_units": work_units,
        "estimated_parent_review_ratio": expected_review_ratio,
        "task_bucket": _task_bucket(role, features),
        "gate_features": features,
        "cost_estimate_status": "legacy_work_units",
    }


def _task_bucket(role: str, features: dict[str, Any]) -> str:
    if features.get("semantic_multimodal"):
        return "multimodal"
    if features.get("deterministic_tool_possible"):
        return "deterministic_tool"
    return {
        "router_scout": "batch_read",
        "router_reviewer": "review",
        "router_worker": "implementation",
        "router_tester": "test",
        "router_docs": "docs",
        "router_monitor": "deterministic_wait",
    }.get(role, "uncertain")


def _v2_economic_decision(
    role: str,
    reasons: list[str],
    write_authorized: bool,
    features: dict[str, Any],
    count: int,
    execution_profile: str,
    light_profile: str,
) -> dict[str, Any]:
    if role == "router_monitor":
        reasons.extend(["gate:deterministic_wait", "policy:v2_static"])
        return {
            "decision": "DELEGATE",
            "role": role,
            "risk": "LOW",
            "confidence": 0.96,
            "reason_codes": reasons,
            "write_authorized": False,
            "estimated_work_units": features["work_units_legacy"],
            "estimated_parent_review_ratio": 0.0,
            "task_bucket": "deterministic_wait",
            "gate_features": features,
            "cost_estimate_status": "tool_only_wait",
        }
    if features["semantic_multimodal"] and role in {"router_worker", "router_reviewer"}:
        reasons.extend(["gate:multimodal_capability", "policy:v2_static"])
        return {
            "decision": "DELEGATE",
            "role": role,
            "risk": "LOW",
            "confidence": 0.94,
            "reason_codes": reasons,
            "write_authorized": write_authorized,
            "estimated_work_units": features["work_units_legacy"],
            "estimated_parent_review_ratio": ROLE_PARENT_REVIEW_CAP[role],
            "task_bucket": "multimodal",
            "gate_features": features,
            "cost_estimate_status": "capability_route",
        }
    if features["deterministic_tool_possible"]:
        kind = str(features["deterministic_tool_kind"])
        return {
            "decision": "TOOL_ONLY",
            "role": None,
            "risk": "LOW",
            "confidence": 0.97,
            "reason_codes": [f"deterministic_tool:{kind}", f"candidate:{role}", "policy:v2_static"],
            "write_authorized": False,
            "estimated_work_units": features["work_units_legacy"],
            "estimated_parent_review_ratio": 0.0,
            "task_bucket": "deterministic_tool",
            "gate_features": features,
            "cost_estimate_status": "tool_fast_path",
        }
    if features["micro_task"]:
        return _inline_candidate(role, features, "hard_inline:micro_task", "policy:v2_static")
    if not features["independent_bounded_package"]:
        return _inline_candidate(role, features, "hard_inline:no_bounded_package", "policy:v2_static", confidence=0.9)
    if role in WRITER_ROLES and features["strong_batch"] and not (
        features["unique_path_count"] >= 2 or features["explicit_item_count"] >= 2
    ):
        return _inline_candidate(role, features, "hard_inline:unbounded_write_scope", "policy:v2_static")

    minimum_items = ROLE_MIN_ITEMS.get(role, 4)
    if light_profile == LIGHT_PROFILE_LOCAL_TEXT_FIRST and role == "router_scout":
        minimum_items = 8
    if execution_profile == PROFILE_GLM_FIRST and role in {"router_worker", "router_reviewer"}:
        minimum_items = 5
    estimated_items = int(features["independent_item_count_estimate"])
    if estimated_items < minimum_items or features["estimated_sol_turns"] != "4+":
        return _inline_candidate(
            role,
            features,
            "static_break_even_proxy:insufficient_scale",
            f"min_items:{minimum_items}",
            "policy:v2_static",
            confidence=0.92,
        )

    review_cap = ROLE_PARENT_REVIEW_CAP.get(role, 0.35)
    if light_profile == LIGHT_PROFILE_LOCAL_TEXT_FIRST and role == "router_scout":
        review_cap = 0.25
    elif execution_profile == PROFILE_GLM_FIRST and role in {"router_worker", "router_reviewer"}:
        review_cap = 0.30
    reasons.extend(
        [
            "gate:bounded_package",
            f"scale:{estimated_items}",
            f"review_cap:{review_cap:.2f}",
            "static_break_even_proxy:pass",
            "quality_guard:hard_rules",
            "policy:v2_static",
        ]
    )
    return {
        "decision": "DELEGATE",
        "role": role,
        "risk": "LOW",
        "confidence": min(0.96, 0.78 + 0.06 * count),
        "reason_codes": reasons,
        "write_authorized": write_authorized,
        "estimated_work_units": features["work_units_legacy"],
        "estimated_parent_review_ratio": review_cap,
        "task_bucket": _task_bucket(role, features),
        "gate_features": features,
        "cost_estimate_status": "cold_start_static_proxy",
    }


def classify(
    prompt: str,
    *,
    economics: bool = False,
    economics_policy: str = DEFAULT_ECONOMICS_POLICY,
    execution_profile: str = PROFILE_STABLE,
    light_profile: str = LIGHT_PROFILE_LUNA_STABLE,
) -> dict[str, Any]:
    text = " ".join(prompt.lower().split())
    risk_reasons = _risk_reasons(text)
    if risk_reasons:
        return {
            "decision": "INLINE_SOL",
            "role": None,
            "risk": "HIGH",
            "confidence": 0.99,
            "reason_codes": risk_reasons,
            "write_authorized": False,
        }

    policy = str(economics_policy or DEFAULT_ECONOMICS_POLICY).upper()
    semantic_multimodal = any(term in text for term in MULTIMODAL_SEMANTIC_TERMS)
    if economics and policy != "V1_COMPAT" and not semantic_multimodal and _deterministic_tool_kind(text):
        features = _economic_features(text, "router_scout", [])
        return _v2_economic_decision(
            "router_scout",
            ["category:deterministic_tool"],
            False,
            features,
            1,
            str(execution_profile).upper(),
            str(light_profile).upper(),
        )

    matches: list[tuple[str, int, list[str]]] = []
    for role, terms in CATEGORY_TERMS.items():
        found = _positive_matches(text, terms)
        if found:
            matches.append((role, len(found), found))

    authorized_writers = {
        role
        for role in WRITER_ROLES
        if write_authorized_for(prompt, role)
    }
    if semantic_multimodal:
        if "router_worker" in authorized_writers:
            matches = [("router_worker", 3, ["semantic_multimodal", "multimodal_write"])]
        else:
            matches = [("router_reviewer", 2, ["semantic_multimodal"])]

    # Least privilege: when a prompt has both a read-only lane and a writer lane
    # without explicit write authorization, discard the writer match. If a writer
    # lane is the only match, retain the model capability but remap it to read-only
    # scout instead of granting workspace-write.
    read_only_matches = [item for item in matches if item[0] not in WRITER_ROLES]
    authorized_writer_matches = [item for item in matches if item[0] in authorized_writers]
    unauthorized_writers = [
        item for item in matches if item[0] in WRITER_ROLES and item[0] not in authorized_writers
    ]
    if authorized_writer_matches and unauthorized_writers:
        matches = [item for item in matches if item not in unauthorized_writers]
    elif read_only_matches and unauthorized_writers:
        matches = [item for item in matches if item not in unauthorized_writers]
    elif unauthorized_writers and not read_only_matches:
        source_role, count, found = max(unauthorized_writers, key=lambda item: item[1])
        matches = [("router_scout", count, found)]

    if matches:
        matches.sort(key=lambda item: item[1], reverse=True)
        role, count, found = matches[0]
        ambiguity = len(matches) > 1 and matches[1][1] == count
        if ambiguity:
            return {
                "decision": "INLINE_SOL",
                "role": None,
                "risk": "MEDIUM",
                "confidence": 0.55,
                "reason_codes": ["ambiguous_categories"],
                "write_authorized": False,
            }
        write_authorized = role in authorized_writers
        reasons = [f"category:{role}", f"matched_terms:{len(found)}"]
        if role == "router_scout" and unauthorized_writers and not read_only_matches:
            reasons.append(f"least_privilege_remap:{unauthorized_writers[0][0]}")
        if write_authorized:
            reasons.append("explicit_write_intent")
        features = _economic_features(text, role, found)
        if economics:
            if policy == "V1_COMPAT":
                return _v1_economic_decision(role, found, reasons, write_authorized, features, count)
            return _v2_economic_decision(
                role,
                reasons,
                write_authorized,
                features,
                count,
                str(execution_profile).upper(),
                str(light_profile).upper(),
            )
        expected_review_ratio = ROLE_PARENT_REVIEW_CAP.get(role, 0.35)
        reasons.extend([f"review_cap:{expected_review_ratio:.2f}"])
        return {
            "decision": "DELEGATE",
            "role": role,
            "risk": "LOW",
            "confidence": min(0.96, 0.78 + 0.06 * count),
            "reason_codes": reasons,
            "write_authorized": write_authorized,
            "estimated_work_units": features["work_units_legacy"],
            "estimated_parent_review_ratio": expected_review_ratio,
            "task_bucket": _task_bucket(role, features),
            "gate_features": features,
            "cost_estimate_status": "not_requested",
        }

    if len(text) < 80:
        reason = "small_task"
    else:
        reason = "uncertain_intent"
    return {
        "decision": "INLINE_SOL",
        "role": None,
        "risk": "LOW" if reason == "small_task" else "MEDIUM",
        "confidence": 0.9 if reason == "small_task" else 0.6,
        "reason_codes": [reason],
        "write_authorized": False,
    }


def prompt_digest(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


def append_telemetry(root: Path, event: dict[str, Any]) -> None:
    telemetry = root / "telemetry.jsonl"
    telemetry.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": int(time.time()), **event}
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(telemetry, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def cleanup_expired(root: Path, now: int | None = None) -> int:
    sessions = root / "sessions"
    if not sessions.is_dir():
        return 0
    cutoff = (now or int(time.time())) - STATE_TTL_SECONDS
    removed = 0
    for path in sessions.glob("*.json"):
        try:
            if int(path.stat().st_mtime) < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


RECEIPT_FIELDS = {
    "schema_version": int,
    "objective_id": str,
    "status": str,
    "summary": str,
    "findings": list,
    "evidence": list,
    "evidence_manifest": list,
    "inconsistencies": list,
    "coverage": dict,
    "parent_verification": list,
    "changed_files": list,
    "validation": list,
    "remaining_risks": list,
    "needs_escalation": bool,
    "recommended_next_action": str,
}

RECEIPT_STRING_LIMITS = {
    "objective_id": 64,
    "summary": 500,
    "recommended_next_action": 300,
}
RECEIPT_ARRAY_LIMITS = {
    "findings": (6, 800),
    "evidence": (6, 800),
    "inconsistencies": (4, 600),
    "parent_verification": (3, 300),
    "changed_files": (50, 300),
    "validation": (6, 600),
    "remaining_risks": (6, 600),
}
RECEIPT_RESERVED_FRAGMENTS = {
    "summary",
    "findings",
    "evidence",
    "evidence_manifest",
    "inconsistencies",
    "coverage",
    "parent_verification",
    "changed_files",
    "validation",
    "remaining_risks",
    "recommended_next_action",
}


def validate_receipt(raw: str) -> tuple[bool, list[str], dict[str, Any] | None]:
    errors: list[str] = []
    try:
        receipt = json.loads(raw.strip())
    except (json.JSONDecodeError, AttributeError):
        return False, ["response must be one JSON object without Markdown fences"], None
    if not isinstance(receipt, dict):
        return False, ["receipt root must be an object"], None
    extra_fields = set(receipt) - set(RECEIPT_FIELDS)
    if extra_fields:
        errors.append("unexpected fields: " + ", ".join(sorted(extra_fields)))
    for field, expected in RECEIPT_FIELDS.items():
        if field not in receipt:
            errors.append(f"missing field: {field}")
        elif not isinstance(receipt[field], expected):
            errors.append(f"{field} must be {expected.__name__}")
    if isinstance(receipt.get("status"), str) and receipt["status"] not in {"completed", "blocked", "failed"}:
        errors.append("status must be completed, blocked, or failed")
    if receipt.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    objective_id = receipt.get("objective_id")
    if isinstance(objective_id, str) and not re.fullmatch(r"[0-9a-f]{64}", objective_id):
        errors.append("objective_id must be a lowercase SHA-256 hex digest")
    for field, limit in RECEIPT_STRING_LIMITS.items():
        value = receipt.get(field)
        if isinstance(value, str) and len(value) > limit:
            errors.append(f"{field} exceeds {limit} characters")
    for field, (item_limit, length_limit) in RECEIPT_ARRAY_LIMITS.items():
        value = receipt.get(field)
        if not isinstance(value, list):
            continue
        if not all(isinstance(item, str) for item in value):
            errors.append(f"{field} items must be strings")
            continue
        if len(value) > item_limit:
            errors.append(f"{field} exceeds {item_limit} items")
        # Exact contact with a schema maxLength is a strong signal that
        # constrained decoding clipped a sentence. Reject it instead of
        # presenting a syntactically valid but semantically broken receipt.
        if any(len(item) >= length_limit for item in value):
            errors.append(f"{field} item reaches the {length_limit}-character truncation guard")
        fragments = {
            re.sub(r"[\s。．.!！:：]+", "", item).lower()
            for item in value
            if isinstance(item, str)
        }
        if fragments & RECEIPT_RESERVED_FRAGMENTS:
            errors.append(f"{field} contains a field-name fragment")
    manifest = receipt.get("evidence_manifest")
    if isinstance(manifest, list):
        if len(manifest) > 6:
            errors.append("evidence_manifest exceeds 6 items")
        for index, item in enumerate(manifest):
            if not isinstance(item, dict):
                errors.append(f"evidence_manifest[{index}] must be an object")
                continue
            if set(item) != {"claim", "path", "locator", "sha256"}:
                errors.append(f"evidence_manifest[{index}] has invalid fields")
                continue
            if not all(isinstance(item[name], str) for name in ("claim", "path", "locator")):
                errors.append(f"evidence_manifest[{index}] text fields must be strings")
            else:
                for name, limit in (("claim", 500), ("path", 300), ("locator", 120)):
                    if len(item[name]) > limit:
                        errors.append(f"evidence_manifest[{index}].{name} exceeds {limit} characters")
            digest = item.get("sha256")
            if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                errors.append(f"evidence_manifest[{index}].sha256 must be null or lowercase SHA-256")
    coverage = receipt.get("coverage")
    if isinstance(coverage, dict):
        if set(coverage) != {"mode", "checked", "total"}:
            errors.append("coverage has invalid fields")
        if coverage.get("mode") not in {"full", "sample", "targeted"}:
            errors.append("coverage.mode must be full, sample, or targeted")
        checked = coverage.get("checked")
        total = coverage.get("total")
        if not isinstance(checked, int) or isinstance(checked, bool) or checked < 0:
            errors.append("coverage.checked must be a non-negative integer")
        if total is not None and (not isinstance(total, int) or isinstance(total, bool) or total < 0):
            errors.append("coverage.total must be null or a non-negative integer")
        if isinstance(checked, int) and isinstance(total, int) and checked > total:
            errors.append("coverage.checked cannot exceed coverage.total")
    return not errors, errors, receipt


def routing_context(
    mode: str,
    decision: dict[str, Any],
    execution_profile: str = PROFILE_STABLE,
    light_profile: str = LIGHT_PROFILE_LUNA_STABLE,
) -> str:
    role = decision.get("role") or "main_sol"
    if mode == "SHADOW":
        if decision["decision"] == "TOOL_ONLY":
            recommendation = "确定性工具 fast path"
        elif decision["decision"] != "DELEGATE":
            recommendation = "Sol"
        elif execution_profile == PROFILE_GLM_FIRST and role in {"router_worker", "router_reviewer"}:
            recommendation = "GLM-5.3 Max / Terra 动态执行"
        elif light_profile == LIGHT_PROFILE_LOCAL_TEXT_FIRST and role == "router_scout":
            recommendation = "Local Text / Luna 动态执行"
        else:
            recommendation = ROLE_LABELS.get(role, "Sol")
        return (
            f"SR_SHADOW recommended={role} risk={decision['risk']}. Do not delegate; handle normally in Sol. "
            f'End the answer with exactly: "路由预览：{recommendation}".'
        )
    if decision["decision"] == "TOOL_ONLY":
        kind = str((decision.get("gate_features") or {}).get("deterministic_tool_kind") or "direct_tool")
        return (
            f"SR_ON TOOL_ONLY kind={kind} risk={decision['risk']}. "
            "Handle in Sol with the minimum direct deterministic tool call; do not spawn or call route_task."
        )
    if decision["decision"] != "DELEGATE":
        return f"SR_ON INLINE_SOL risk={decision['risk']}. Do not delegate; handle normally in Sol without a route label."
    write_flag = "1" if decision.get("write_authorized") else "0"
    decision_id = str(decision.get("decision_id") or "")
    lease_id = str(decision.get("lease_id") or "")
    if role == "router_monitor":
        return (
            f"SR_ON WAIT decision_id={decision_id} lease_id={lease_id} risk={decision['risk']}. "
            "Use smart_router.wait_for_condition exactly once with these IDs; do not spawn an agent or poll. "
            "The MCP call blocks deterministically until the condition, timeout, or cancellation, then this Sol turn resumes."
        )
    batch_instruction = (
        " Batch 4-12 same-goal read-only items into this call."
        if (decision.get("gate_features") or {}).get("coalesce_candidate")
        else ""
    )
    image_instruction = (
        " Include required image paths to force Terra."
        if (decision.get("gate_features") or {}).get("semantic_multimodal")
        else ""
    )
    return (
        f"SR_ON DELEGATE decision_id={decision_id} lease_id={lease_id} role={role} profile={execution_profile} light={light_profile} write={write_flag}. "
        "Call smart_router.route_task once with exact values; no subagents."
        f"{batch_instruction}{image_instruction} "
        "Verify only receipt checks/anomalies/sample; do not reread all. "
        "Success: append `路由：` + receipt._router_meta.route_label. "
        'Error: finish exactly "路由回退：Sol（委派未完成）".'
    )
