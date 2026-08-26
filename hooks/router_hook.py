#!/usr/bin/env python3
"""Unified Codex hook entry point. Fail-open on hook errors, fail-safe on routing."""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from provider_policy import (  # noqa: E402
    LIGHT_PROFILE_LOCAL_TEXT_FIRST,
    LIGHT_PROFILE_LUNA_STABLE,
    PROFILE_GLM_FIRST,
    PROFILE_STABLE,
    glm_key,
    read_health,
)
from local_provider import (  # noqa: E402
    load_config as load_local_config,
    provider_key as local_provider_key,
    read_health as read_local_health,
)

from router_core import (  # noqa: E402
    ROLES,
    ROLE_LABELS,
    WRITER_ROLES,
    append_telemetry,
    classify,
    cleanup_expired,
    data_root,
    delegation_task_digest,
    load_state,
    parse_control,
    prompt_digest,
    routing_context,
    save_state,
    session_state_lock,
    set_execution_profile,
    set_light_profile,
    set_mode,
    validate_receipt,
    writer_lock_held,
)

WRAPPER_TOOL_NAMES = {
    "mcp__smart_router__route_task",
    "smart_router__route_task",
}
WAIT_TOOL_NAMES = {
    "mcp__smart_router__wait_for_condition",
    "smart_router__wait_for_condition",
}
ROUTER_TOOL_NAMES = WRAPPER_TOOL_NAMES | WAIT_TOOL_NAMES
NATIVE_AGENT_TOOL_NAMES = {"Agent", "spawn_agent"}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def hook_context(event: str, text: str) -> None:
    emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def deny_pretool(reason: str) -> None:
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def session_id(payload: dict[str, Any]) -> str:
    value = payload.get("session_id") or payload.get("conversation_id")
    return str(value) if value else "unknown-session"


def status_text(state: dict[str, Any]) -> str:
    last = state.get("last_decision")
    profile = str(state.get("execution_profile") or PROFILE_STABLE)
    light_profile = str(state.get("light_profile") or LIGHT_PROFILE_LUNA_STABLE)
    if isinstance(last, dict):
        last_role = str(last.get("role") or "")
        if profile == PROFILE_GLM_FIRST and last_role in {"router_worker", "router_reviewer"}:
            last_text = "GLM-5.3 Max / Terra 动态执行"
        elif light_profile == LIGHT_PROFILE_LOCAL_TEXT_FIRST and last_role == "router_scout":
            config, _ = load_local_config()
            last_text = f"{config.display_name if config else 'Local Text'} / Luna 动态执行"
        else:
            last_text = ROLE_LABELS.get(last_role, "Sol")
    else:
        last_text = "暂无"
    _, installed, wrapper_ready, parked = environment_details()
    ready = installed == len(ROLES) and wrapper_ready and not parked
    mode_labels = {"OFF": "已关闭", "SHADOW": "影子模式", "ON": "已开启"}
    profile_text = "GLM_FIRST" if profile == PROFILE_GLM_FIRST else "STABLE"
    light_profile_text = (
        "LOCAL_TEXT_FIRST" if light_profile == LIGHT_PROFILE_LOCAL_TEXT_FIRST else "LUNA_STABLE"
    )
    if ready:
        environment = "就绪"
    elif parked:
        environment = "已停用（可用安装器 --enable 恢复）"
    else:
        environment = f"未就绪（agent {installed}/{len(ROLES)}，wrapper {'已安装' if wrapper_ready else '缺失'}）"
    counts = state.get("execution_counts") or {}
    completed = int(counts.get("completed", 0))
    failed = int(counts.get("failed", 0))
    last_execution = state.get("last_execution")
    if isinstance(last_execution, dict):
        outcome = "成功" if last_execution.get("outcome") == "completed" else "失败"
        label = str(last_execution.get("route_label") or ROLE_LABELS.get(str(last_execution.get("role")), "未知角色"))
        actual = f"最近实际执行：{label}（{outcome}）"
    else:
        actual = "本会话尚无实际委派"
    writer = "忙碌" if isinstance(state.get("active_writer"), dict) else "空闲"
    if profile == PROFILE_GLM_FIRST:
        health = read_health()
        if not glm_key():
            glm_status = "GLM Key 未配置，将用 Terra"
        elif health.get("state") == "closed":
            glm_status = "GLM 可用（高峰时段自动改用 Terra）"
        else:
            glm_status = f"GLM 熔断：{health.get('reason') or 'unknown'}"
    else:
        glm_status = "GLM 未启用"
    if light_profile == LIGHT_PROFILE_LOCAL_TEXT_FIRST:
        local_config, local_reason = load_local_config()
        if local_config is None:
            local_status = f"本地文本模型未就绪（{local_reason}），将用 Luna"
        elif local_config.env_key and not local_provider_key(local_config):
            local_status = f"{local_config.display_name} Key 未配置，将用 Luna"
        else:
            local_health = read_local_health()
            if local_health.get("state") == "closed":
                local_status = f"{local_config.display_name} 可用（只读轻任务）"
            else:
                local_status = f"{local_config.display_name} 熔断，将用 Luna"
    else:
        local_status = "本地文本模型未启用"
    return (
        f"智能路由：{mode_labels[state['mode']]}（仅当前会话）｜重任务：{profile_text}｜轻任务：{light_profile_text}｜"
        f"{glm_status}｜{local_status}｜环境：{environment}｜"
        f"最近建议：{last_text}｜{actual}｜累计实际执行：成功 {completed}，失败 {failed}｜写入槽：{writer}。"
    )


def environment_details() -> tuple[Path, int, bool, bool]:
    configured = os.environ.get("CODEX_HOME")
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    installed = sum((codex_home / "agents" / f"{role}.toml").is_file() for role in ROLES)
    try:
        config = (codex_home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        config = ""
    wrapper_ready = "[mcp_servers.smart_router]" in config
    parked = (codex_home / "smart-router" / "DISABLED").exists()
    return codex_home, installed, wrapper_ready, parked


def on_session_start(payload: dict[str, Any]) -> None:
    root = data_root()
    cleanup_expired(root)
    state = load_state(root, session_id(payload))
    _, installed, wrapper_ready, parked = environment_details()
    if parked:
        command = PLUGIN_ROOT / "scripts" / "install_agents.py"
        hook_context(
            "SessionStart",
            f'SMART_ROUTER_SETUP: 本地路由已停用。仅在合适时提醒用户运行 python3 "{command}" --enable；不要自动恢复。',
        )
    elif installed != len(ROLES) or not wrapper_ready:
        command = PLUGIN_ROOT / "scripts" / "install_agents.py"
        hook_context(
            "SessionStart",
            f'SMART_ROUTER_SETUP: 环境尚未就绪。仅在合适时提醒用户运行 python3 "{command}" --apply；不要自动安装。',
        )
    elif state["mode"] != "OFF":
        hook_context("SessionStart", f"SR_SESSION mode={state['mode']} restored for this session.")


def on_user_prompt(payload: dict[str, Any]) -> None:
    sid = session_id(payload)
    root = data_root()
    prompt = str(payload.get("prompt") or "")
    action = parse_control(prompt)
    state = load_state(root, sid)
    active = state.get("active_writer")
    if isinstance(active, dict) and active.get("source") == "mcp":
        workspace = active.get("cwd")
        # PostToolUse is primary. If that event was lost, recover only after an
        # OS-level workspace lock proves no writable child is still running.
        # The wrapper uses the same lock, so a steered prompt cannot release a
        # live writer merely because it starts another user turn.
        if not workspace or not writer_lock_held(str(workspace)):
            state["active_writer"] = None
            save_state(root, sid, state)
            append_telemetry(
                root,
                {
                    "event": "writer_recovered_after_lock_check",
                    "role": active.get("role"),
                    "session": state["session_key"][:12],
                },
            )
    if action in {"ON", "SHADOW", "OFF", "GLM_ON", "GLM_OFF", "LOCAL_ON", "LOCAL_OFF"}:
        if action in {"ON", "GLM_ON", "LOCAL_ON"}:
            _, installed, wrapper_ready, parked = environment_details()
            command = PLUGIN_ROOT / "scripts" / "install_agents.py"
            if parked:
                reply = f'智能路由尚未开启：本地路由已停用。请先运行 python3 "{command}" --enable。'
                hook_context("UserPromptSubmit", f'SMART_ROUTER_UI_REPLY: 请原样回复："{reply}"')
                return
            if installed != len(ROLES) or not wrapper_ready:
                reply = f'智能路由尚未开启：环境未就绪。请先运行 python3 "{command}" --apply。'
                hook_context("UserPromptSubmit", f'SMART_ROUTER_UI_REPLY: 请原样回复："{reply}"')
                return
        if action == "GLM_ON":
            state = set_execution_profile(root, sid, PROFILE_GLM_FIRST, activate=True)
        elif action == "GLM_OFF":
            state = set_execution_profile(root, sid, PROFILE_STABLE)
        elif action == "LOCAL_ON":
            state = set_light_profile(root, sid, LIGHT_PROFILE_LOCAL_TEXT_FIRST, activate=True)
        elif action == "LOCAL_OFF":
            state = set_light_profile(root, sid, LIGHT_PROFILE_LUNA_STABLE)
        else:
            state = set_mode(root, sid, action)
        append_telemetry(
            root,
            {
                "event": "routing_control_changed",
                "mode": state["mode"],
                "execution_profile": state["execution_profile"],
                "light_profile": state["light_profile"],
                "session": state["session_key"][:12],
            },
        )
        replies = {
            "ON": "已开启智能路由（仅当前会话）。当前保留原执行配置；边界清晰且适合委派的轻任务交给 Luna，高风险或不确定任务仍由 Sol 处理。",
            "SHADOW": "已开启影子模式（仅当前会话）。后续只显示路由预览，不会实际委派。",
            "OFF": "已关闭智能路由（仅当前会话）。后续任务全部由 Sol 处理。",
            "GLM_ON": "已开启 GLM_FIRST 智能路由（仅当前会话）。Luna 处理轻任务；复杂纯文本任务优先 GLM-5.3 Max；工作日 14:00–18:00、额度熔断或多模态时自动改用 Terra。",
            "GLM_OFF": "已关闭当前会话的 GLM_FIRST，恢复 STABLE 执行配置；智能路由的 ON/OFF 状态不变。",
            "LOCAL_ON": "已开启 LOCAL_TEXT_FIRST（仅当前会话）。批量只读侦察优先使用已配置的本地文本模型，失败或不可用时自动回退 Luna；测试和文档仍由 Luna 处理，等待任务始终使用无模型的确定性长等待。",
            "LOCAL_OFF": "已关闭当前会话的 LOCAL_TEXT_FIRST，恢复 LUNA_STABLE；智能路由的 ON/OFF 状态不变。",
        }
        hook_context("UserPromptSubmit", f'SMART_ROUTER_UI_REPLY: 请原样回复："{replies[action]}"')
        return
    if action in {"STATUS", "HELP"}:
        if action == "HELP":
            reply = (
                "当前会话可用命令：$router-control 开启、glm 开启、glm 关闭、local 开启、local 关闭、影子模式、关闭、状态。"
                "开启后自动判断；影子模式只预览；全局关闭请使用 /plugins。"
            )
        else:
            reply = status_text(state)
        hook_context("UserPromptSubmit", f'SMART_ROUTER_UI_REPLY: 请原样回复："{reply}"')
        return
    if state["mode"] == "OFF":
        return
    decision = classify(prompt, economics=True)
    decision_id = prompt_digest(prompt)
    lease_id = secrets.token_hex(16)
    decision["decision_id"] = decision_id
    decision["lease_id"] = lease_id
    state["last_decision"] = decision
    state["repair_attempts"] = 0
    state["current_delegation"] = {
        "decision_id": decision_id,
        "lease_id": lease_id,
        "status": "available" if decision["decision"] == "DELEGATE" else "not_applicable",
        "source": None,
        "tool_use_id": None,
    }
    save_state(root, sid, state)
    append_telemetry(
        root,
        {
            "event": "route_decision",
            "mode": state["mode"],
            "decision": decision["decision"],
            "role": decision.get("role"),
            "execution_profile": state["execution_profile"],
            "light_profile": state["light_profile"],
            "risk": decision["risk"],
            "reason_codes": decision["reason_codes"],
            "estimated_work_units": decision.get("estimated_work_units"),
            "estimated_parent_review_ratio": decision.get("estimated_parent_review_ratio"),
            "prompt_sha256": decision_id,
            "session": state["session_key"][:12],
        },
    )
    hook_context(
        "UserPromptSubmit",
        routing_context(state["mode"], decision, state["execution_profile"], state["light_profile"]),
    )


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("tool_input") or payload.get("input") or {}
    return value if isinstance(value, dict) else {}


def _receipt_status(response: Any) -> str | None:
    """Extract a child receipt status from common MCP result shapes."""
    if not isinstance(response, dict):
        return None
    structured = response.get("structuredContent") or response.get("structured_content")
    if isinstance(structured, dict) and structured.get("status") in {"completed", "blocked", "failed", "timeout", "cancelled"}:
        return str(structured["status"])
    content = response.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            try:
                parsed = json.loads(item["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("status") in {"completed", "blocked", "failed", "timeout", "cancelled"}:
                return str(parsed["status"])
    return None


def _receipt_meta(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    structured = response.get("structuredContent") or response.get("structured_content")
    if isinstance(structured, dict) and isinstance(structured.get("_router_meta"), dict):
        return dict(structured["_router_meta"])
    content = response.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            try:
                parsed = json.loads(item["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("_router_meta"), dict):
                return dict(parsed["_router_meta"])
    return {}


def _claim_delegation(
    root: Path,
    sid: str,
    *,
    decision_id: str,
    lease_id: str,
    role: str,
    source: str,
    tool_use_id: str,
    task_digest: str,
    require_role_match: bool,
    writer_record: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Atomically compare-and-swap the one delegation slot."""
    with session_state_lock(root, sid):
        state = load_state(root, sid)
        decision = state.get("last_decision") or {}
        delegation = state.get("current_delegation") or {}
        if state.get("mode") != "ON" or decision.get("decision") != "DELEGATE":
            return False, "routing_not_active", state
        if decision.get("decision_id") != decision_id or decision.get("lease_id") != lease_id:
            return False, "decision_mismatch", state
        if require_role_match and decision.get("role") != role:
            return False, "role_mismatch", state
        if (
            delegation.get("decision_id") != decision_id
            or delegation.get("lease_id") != lease_id
            or delegation.get("status") != "available"
        ):
            return False, "already_consumed", state
        if writer_record is not None:
            active = state.get("active_writer")
            if isinstance(active, dict) and not active.get("tool_use_id"):
                active = None
                state["active_writer"] = None
            if isinstance(active, dict) and int(time.time()) - int(active.get("started_at", 0)) < 1800:
                return False, "writer_active", state
            state["active_writer"] = writer_record
        delegation.update(
            {
                "status": "started",
                "source": source,
                "role": role,
                "tool_use_id": tool_use_id,
                "task_digest": task_digest,
                "started_at": int(time.time()),
            }
        )
        state["current_delegation"] = delegation
        save_state(root, sid, state)
        return True, "claimed", state


def on_pre_tool(payload: dict[str, Any]) -> None:
    tool_input = _tool_input(payload)
    tool_name = str(payload.get("tool_name") or payload.get("name") or "")
    is_wrapper = tool_name in WRAPPER_TOOL_NAMES
    is_wait = tool_name in WAIT_TOOL_NAMES
    is_router_tool = tool_name in ROUTER_TOOL_NAMES
    role = str(tool_input.get("agent_type") or tool_input.get("name") or tool_input.get("task_name") or "")
    if is_wrapper:
        role = str(tool_input.get("role") or "")
    elif is_wait:
        role = "router_monitor"
    is_native_agent = not is_router_tool and (
        bool(role) or tool_name in NATIVE_AGENT_TOOL_NAMES or tool_name.endswith("__spawn_agent")
    )
    if is_native_agent and not role:
        role = "external_agent"
    if is_native_agent and role.startswith("router_"):
        deny_pretool(
            "v0.4 routing roles are synchronous MCP-only so the main Sol turn resumes automatically; "
            "use smart_router.route_task or smart_router.wait_for_condition."
        )
        return
    if is_native_agent:
        root = data_root()
        sid = session_id(payload)
        state = load_state(root, sid)
        if state["mode"] != "ON":
            return
        decision = state.get("last_decision") or {}
        if decision.get("decision") != "DELEGATE":
            return
        delegation = state.get("current_delegation") or {}
        claimed, reason, state = _claim_delegation(
            root,
            sid,
            decision_id=str(decision.get("decision_id") or ""),
            lease_id=str(decision.get("lease_id") or delegation.get("lease_id") or ""),
            role=role,
            source="external_native_agent",
            tool_use_id=str(payload.get("tool_use_id") or ""),
            task_digest=delegation_task_digest(tool_name, tool_input),
            require_role_match=False,
        )
        if not claimed:
            deny_pretool("This objective already used its single delegation slot; integrate the existing receipt instead.")
            return
        append_telemetry(
            root,
            {
                "event": "external_delegation_consumed_budget",
                "role": role,
                "decision_id": str(decision.get("decision_id") or "")[:12],
                "session": state["session_key"][:12],
            },
        )
        return
    if role not in ROLES:
        deny_pretool(f"Smart Router refused unknown agent role: {role}")
        return
    root = data_root()
    sid = session_id(payload)
    state = load_state(root, sid)
    if state["mode"] != "ON":
        deny_pretool(f"Smart Router mode is {state['mode']}; routing agents require ON.")
        return
    decision = state.get("last_decision") or {}
    delegation = state.get("current_delegation") or {}
    if decision.get("decision") != "DELEGATE" or decision.get("role") != role:
        deny_pretool("Requested routing agent does not match the current safety decision.")
        return
    decision_id = str(tool_input.get("decision_id") or "")
    if not decision_id or decision_id != decision.get("decision_id"):
        deny_pretool("Requested routing call does not match the current decision_id.")
        return
    lease_id = str(tool_input.get("lease_id") or "")
    if not lease_id or lease_id != decision.get("lease_id"):
        deny_pretool("Requested routing call does not match the current one-time lease_id.")
        return
    if delegation.get("decision_id") != decision_id or delegation.get("status") != "available":
        deny_pretool("This objective already used its single delegation slot; integrate the existing receipt instead.")
        return
    if role == "router_monitor" and not is_wait:
        deny_pretool("Monitoring must use smart_router.wait_for_condition; model-based monitoring is disabled in v0.4.")
        return
    if role != "router_monitor" and is_wait:
        deny_pretool("wait_for_condition is only valid for a router_monitor decision.")
        return
    requested_profile = str(tool_input.get("execution_profile") or PROFILE_STABLE).upper()
    if is_wrapper and requested_profile != state.get("execution_profile", PROFILE_STABLE):
        deny_pretool("Requested execution profile does not match the current session profile.")
        return
    requested_light_profile = str(
        tool_input.get("light_profile") or LIGHT_PROFILE_LUNA_STABLE
    ).upper()
    if is_wrapper and requested_light_profile != state.get("light_profile", LIGHT_PROFILE_LUNA_STABLE):
        deny_pretool("Requested light profile does not match the current session light profile.")
        return
    if (
        not is_wrapper
        and state.get("execution_profile") == PROFILE_GLM_FIRST
        and role in {"router_worker", "router_reviewer"}
    ):
        deny_pretool("GLM_FIRST worker/reviewer tasks must use smart_router.route_task for dynamic provider selection.")
        return
    if (
        not is_wrapper
        and state.get("light_profile") == LIGHT_PROFILE_LOCAL_TEXT_FIRST
        and role == "router_scout"
    ):
        deny_pretool("LOCAL_TEXT_FIRST scout tasks must use smart_router.route_task for dynamic fallback.")
        return
    images = tool_input.get("images") or []
    if is_wrapper and images and role in {"router_scout", "router_monitor", "router_tester", "router_docs"}:
        deny_pretool("Image inputs require a Terra-capable worker or reviewer role, not a Luna role.")
        return
    if role in WRITER_ROLES and not decision.get("write_authorized"):
        deny_pretool("Writable routing agents require explicit positive write authorization in the current task.")
        return
    now = int(time.time())
    writer_record = None
    if role in WRITER_ROLES:
        tool_use_id = str(payload.get("tool_use_id") or "")
        if not tool_use_id:
            deny_pretool("Writable routing agents require a tool_use_id for lifecycle-safe lease release.")
            return
        writer_record = {
            "role": role,
            "started_at": now,
            "tool_use_id": tool_use_id,
            "turn_id": str(payload.get("turn_id") or ""),
            "cwd": str(payload.get("cwd") or os.getcwd()),
            "source": "mcp",
        }
    claimed, claim_reason, _ = _claim_delegation(
        root,
        sid,
        decision_id=decision_id,
        lease_id=lease_id,
        role=role,
        source="deterministic_wait" if is_wait else "mcp_route_task",
        tool_use_id=str(payload.get("tool_use_id") or ""),
        task_digest=delegation_task_digest(tool_name, tool_input),
        require_role_match=True,
        writer_record=writer_record,
    )
    if not claimed:
        if claim_reason == "writer_active":
            deny_pretool("Another writable routing agent is already active.")
        else:
            deny_pretool("This objective already used its single delegation slot; integrate the existing receipt instead.")


def on_post_tool(payload: dict[str, Any]) -> None:
    """Record actual wrapper execution and release an exactly matched writer lease."""
    tool_name = str(payload.get("tool_name") or payload.get("name") or "")
    if tool_name not in ROUTER_TOOL_NAMES:
        return
    tool_input = _tool_input(payload)
    role = "router_monitor" if tool_name in WAIT_TOOL_NAMES else str(tool_input.get("role") or "")
    if role not in ROLES:
        return
    tool_use_id = str(payload.get("tool_use_id") or "")
    if not tool_use_id:
        return
    root = data_root()
    sid = session_id(payload)
    state = load_state(root, sid)
    execution_key = f"{role}:{tool_use_id}"
    recent = state.get("recent_execution_keys") or []
    if execution_key in recent:
        return
    active = state.get("active_writer")
    if role in WRITER_ROLES and not (
        isinstance(active, dict)
        and active.get("source") == "mcp"
        and active.get("tool_use_id") == tool_use_id
        and active.get("role") == role
    ):
        return
    response = payload.get("tool_response")
    receipt_status = _receipt_status(response)
    receipt_meta = _receipt_meta(response)
    failed = (
        isinstance(response, dict)
        and bool(response.get("isError") or response.get("is_error"))
    ) or receipt_status in {"blocked", "failed", "timeout", "cancelled"}
    outcome = "failed" if failed else "completed"
    counts = state["execution_counts"]
    counts[outcome] = int(counts.get(outcome, 0)) + 1
    state["last_execution"] = {
        "role": role,
        "outcome": outcome,
        "tool_use_id": tool_use_id,
        "at": int(time.time()),
        "model": receipt_meta.get("model"),
        "route_label": receipt_meta.get("route_label"),
        "provider": receipt_meta.get("provider"),
        "fallback_reason": receipt_meta.get("fallback_reason"),
        "usage": receipt_meta.get("usage"),
        "duration_ms": receipt_meta.get("duration_ms"),
    }
    state["recent_execution_keys"] = [*recent, execution_key][-128:]
    released = False
    if role in WRITER_ROLES:
        state["active_writer"] = None
        released = True
    delegation = state.get("current_delegation")
    if (
        isinstance(delegation, dict)
        and delegation.get("decision_id") == tool_input.get("decision_id")
        and delegation.get("tool_use_id") == tool_use_id
    ):
        delegation["status"] = outcome
        delegation["finished_at"] = int(time.time())
        state["current_delegation"] = delegation
    save_state(root, sid, state)
    append_telemetry(
        root,
        {
            "event": "route_execution_finished",
            "role": role,
            "outcome": outcome,
            "receipt_status": receipt_status,
            "writer_released": released,
            "decision_id": str(tool_input.get("decision_id") or "")[:12],
            "usage": receipt_meta.get("usage"),
            "duration_ms": receipt_meta.get("duration_ms"),
            "session": state["session_key"][:12],
        },
    )


def on_subagent_stop(payload: dict[str, Any]) -> None:
    role = str(payload.get("agent_type") or "")
    if role not in ROLES:
        return
    root = data_root()
    sid = session_id(payload)
    state = load_state(root, sid)
    raw = str(payload.get("last_assistant_message") or "")
    valid, errors, receipt = validate_receipt(raw)
    if valid:
        state["repair_attempts"] = 0
        active = state.get("active_writer")
        if role in WRITER_ROLES and isinstance(active, dict) and active.get("source") == "native_agent":
            state["active_writer"] = None
        save_state(root, sid, state)
        append_telemetry(
            root,
            {
                "event": "receipt_valid",
                "role": role,
                "status": receipt.get("status") if receipt else None,
                "session": state["session_key"][:12],
            },
        )
        return
    attempts = int(state.get("repair_attempts", 0))
    if not payload.get("stop_hook_active") and attempts < 1:
        state["repair_attempts"] = attempts + 1
        save_state(root, sid, state)
        emit(
            {
                "decision": "block",
                "reason": "Return exactly one valid JSON receipt. Problems: " + "; ".join(errors),
            }
        )
        return
    active = state.get("active_writer")
    if role in WRITER_ROLES and isinstance(active, dict) and active.get("source") == "native_agent":
        state["active_writer"] = None
    save_state(root, sid, state)
    append_telemetry(
        root,
        {"event": "receipt_invalid_allowed", "role": role, "session": state["session_key"][:12]},
    )


HANDLERS = {
    "session-start": on_session_start,
    "user-prompt-submit": on_user_prompt,
    "pre-tool-use": on_pre_tool,
    "post-tool-use": on_post_tool,
    "subagent-stop": on_subagent_stop,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in HANDLERS:
        print("usage: router_hook.py " + "|".join(HANDLERS), file=sys.stderr)
        return 2
    try:
        HANDLERS[sys.argv[1]](read_input())
    except Exception as exc:  # hooks must not break normal Codex work
        print(f"codex-smart-router hook warning: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
