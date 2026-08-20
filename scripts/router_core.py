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

PLUGIN_ID = "codex-smart-router"
MODES = {"OFF", "SHADOW", "ON"}
ROLES = {
    "router_scout",
    "router_worker",
    "router_reviewer",
    "router_monitor",
    "router_tester",
    "router_docs",
}
WRITER_ROLES = {"router_worker", "router_tester", "router_docs"}
ROLE_LABELS = {
    "router_scout": "Luna · 只读侦察",
    "router_worker": "Terra · 执行",
    "router_reviewer": "Terra · 审查",
    "router_monitor": "Luna · 监控",
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
    "architecture": ("系统架构", "跨系统", "架构决策", "architecture", "distributed transaction", "一致性"),
    "concurrency": ("并发", "竞态", "死锁", "race condition", "deadlock", "事务隔离"),
}

CATEGORY_TERMS = {
    "router_monitor": ("等待", "轮询", "监控", "状态检查", "watch", "poll", "monitor", "babysit"),
    "router_reviewer": ("代码审查", "复核", "review", "找风险", "审阅", "audit"),
    "router_tester": ("测试", "用例", "test", "pytest", "unittest", "验证失败", "跑一下测试"),
    "router_docs": ("文档", "readme", "说明书", "注释更新", "documentation", "changelog"),
    "router_scout": ("搜索", "查找", "盘点", "调研代码", "读日志", "日志分析", "定位文件", "收集证据", "scan", "search", "inventory", "inspect logs"),
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

WRITE_INTENT_PATTERNS = {
    "router_worker": (
        re.compile(r"实现|修复|改代码|重构|新增功能|\b(?:implement|fix|refactor|patch)\b", re.I),
        re.compile(r"创建|新建|写入|\b(?:create|write)\b", re.I),
        re.compile(r"(?:创建|新建|新增|修改|更新).{0,10}(?:文件|模块|功能|代码|脚本)", re.I),
    ),
    "router_tester": (
        re.compile(r"(?:补充|新增|添加|编写|写|完善|修复).{0,10}(?:测试|用例)", re.I),
        re.compile(r"(?:运行|执行|跑).{0,6}(?:测试|pytest|unittest)", re.I),
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


def default_state(session_id: str) -> dict[str, Any]:
    now = int(time.time())
    return {
        "schema_version": 3,
        "session_key": session_key(session_id),
        "mode": "OFF",
        "created_at": now,
        "updated_at": now,
        "last_decision": None,
        "repair_attempts": 0,
        "active_writer": None,
        "execution_counts": {"completed": 0, "failed": 0},
        "last_execution": None,
        "recent_execution_keys": [],
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
    state["schema_version"] = 3
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
    if SECURITY_AUTHORIZATION_ACTION.search(scoped) and "high_risk:security" not in reasons:
        reasons.append("high_risk:security")
    return reasons


def write_authorized_for(prompt: str, role: str) -> bool:
    """Require a positive, non-negated write action for every writable role."""
    if role not in WRITER_ROLES:
        return False
    text = " ".join(prompt.lower().split())
    return (
        not _risk_reasons(text)
        and not _explicit_read_only(text)
        and bool(_write_intent_matches(text, role))
    )


def classify(prompt: str) -> dict[str, Any]:
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
        return {
            "decision": "DELEGATE",
            "role": role,
            "risk": "LOW",
            "confidence": min(0.96, 0.78 + 0.06 * count),
            "reason_codes": reasons,
            "write_authorized": write_authorized,
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
    "status": str,
    "summary": str,
    "findings": list,
    "evidence": list,
    "changed_files": list,
    "validation": list,
    "remaining_risks": list,
    "needs_escalation": bool,
    "recommended_next_action": str,
}

RECEIPT_STRING_LIMITS = {
    "summary": 500,
    "recommended_next_action": 300,
}
RECEIPT_ARRAY_LIMITS = {
    "findings": (6, 800),
    "evidence": (6, 800),
    "changed_files": (50, 300),
    "validation": (6, 600),
    "remaining_risks": (6, 600),
}
RECEIPT_RESERVED_FRAGMENTS = {
    "summary",
    "findings",
    "evidence",
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
    for field, expected in RECEIPT_FIELDS.items():
        if field not in receipt:
            errors.append(f"missing field: {field}")
        elif not isinstance(receipt[field], expected):
            errors.append(f"{field} must be {expected.__name__}")
    if isinstance(receipt.get("status"), str) and receipt["status"] not in {"completed", "blocked", "failed"}:
        errors.append("status must be completed, blocked, or failed")
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
    return not errors, errors, receipt


def routing_context(mode: str, decision: dict[str, Any]) -> str:
    role = decision.get("role") or "main_sol"
    if mode == "SHADOW":
        recommendation = ROLE_LABELS.get(role, "Sol") if decision["decision"] == "DELEGATE" else "Sol"
        return (
            f"SR_SHADOW recommended={role} risk={decision['risk']}. Do not delegate; handle normally in Sol. "
            f'End the answer with exactly: "路由预览：{recommendation}".'
        )
    if decision["decision"] != "DELEGATE":
        return f"SR_ON INLINE_SOL risk={decision['risk']}. Do not delegate; handle normally in Sol without a route label."
    label = ROLE_LABELS[role]
    write_flag = "1" if decision.get("write_authorized") else "0"
    return (
        f"SR_ON DELEGATE role={role} write={write_flag}. Call smart_router.route_task once with this exact role "
        "and a bounded self-contained task; no nested delegation. Integrate its receipt. "
        f'Only if receipt status=completed, end with exactly: "路由：{label}". If status is blocked/failed, '
        "the tool errors, or runtime is unavailable, continue in Sol and end with "
        'exactly: "路由回退：Sol（委派未完成）".'
    )
