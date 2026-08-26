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
    LUNA_BOUNDED,
    LUNA_DISABLED,
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
    DEFAULT_ECONOMICS_POLICY,
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
    recommended_executor_label,
    routing_context,
    save_state,
    session_state_lock,
    set_execution_profile,
    set_economics_policy,
    set_light_profile,
    set_luna_mode,
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


SELECTION_BYPASS_SHORT_REASONS = {
    "local_config_missing": "配置缺失",
    "local_key_missing": "Key 未配置",
    "local_config_key_missing": "Key 未配置",
    "local_config_unsafe_permissions": "配置文件权限异常",
    "local_config_unsafe_file_type": "配置文件类型异常",
    "local_config_unreadable": "配置不可读",
    "local_config_model_catalog_missing": "模型目录缺失",
    "local_config_model_catalog_invalid": "模型目录非法",
    "local_config_model_catalog_mismatch": "模型目录不一致",
    "local_circuit_open": "熔断",
    "local_runtime_failure": "熔断（运行失败）",
    "local_authentication": "熔断（鉴权）",
    "local_probe_in_progress": "半开探针进行中",
    "invalid_policy": "policy 非法",
    "glm_invalid_policy": "policy 非法",
    "glm_peak_window": "高峰时段",
    "glm_key_missing": "Key 未配置",
    "glm_circuit_open": "熔断",
    "glm_quota_5h": "额度熔断（5 小时）",
    "glm_quota_7d": "额度熔断（7 天）",
    "glm_authentication": "鉴权熔断",
    "glm_subscription": "订阅异常",
    "glm_probe_in_progress": "半开探针进行中",
    "multimodal_requires_terra": "多模态需 Terra",
}


def _bypass_provider_label(code: str) -> str:
    return "Local" if code.startswith("local") else "GLM"


def _bypass_entry(code: Any) -> str:
    text = str(code or "")
    reason = SELECTION_BYPASS_SHORT_REASONS.get(text, text or "未知原因")
    return f"{_bypass_provider_label(text)} {reason}"


def _bypass_notes_text(last_execution: dict[str, Any]) -> str | None:
    plural = last_execution.get("selection_bypass_reasons")
    if isinstance(plural, dict) and plural:
        # State persistence sorts keys alphabetically; render in chain-priority
        # order (Local before GLM) regardless of insertion or storage order.
        priority = {"local": 0, "glm": 1}
        codes = [code for _, code in sorted(plural.items(), key=lambda item: priority.get(item[0], 2))]
    elif last_execution.get("selection_bypass_reason"):
        codes = [last_execution["selection_bypass_reason"]]
    else:
        return None
    return "；".join(_bypass_entry(code) for code in codes)


def last_execution_text(last_execution: Any) -> str:
    if not isinstance(last_execution, dict):
        return "本会话尚无实际委派"
    outcome = "成功" if last_execution.get("outcome") == "completed" else "失败"
    path_label = str(
        last_execution.get("route_path_label")
        or last_execution.get("route_label")
        or ROLE_LABELS.get(str(last_execution.get("role")), "未知角色")
    )
    parts = [f"最近实际执行：{path_label}（{outcome}）"]
    bypass = _bypass_notes_text(last_execution)
    if bypass:
        parts.append(f"未尝试：{bypass}")
    reason_code = last_execution.get("fallback_reason_code")
    if reason_code:
        if last_execution.get("fallback_stage") == "deadline":
            parts.append(f"回退未启动原因：{reason_code}")
        elif last_execution.get("fallback_occurred"):
            parts.append(f"回退原因：{reason_code}")
        elif outcome == "失败":
            parts.append(f"失败原因：{reason_code}")
    for attempt in (last_execution.get("attempt_usage") or [])[:2]:
        label = str(attempt.get("model_label") or attempt.get("executor") or "?")
        state = "成功" if attempt.get("outcome") == "completed" else "失败"
        usage = attempt.get("usage") or {}
        seconds = round(int(attempt.get("duration_ms") or 0) / 1000, 1)
        parts.append(
            f"{label}：{state}｜{seconds}s｜input {int(usage.get('input_tokens') or 0)}｜output {int(usage.get('output_tokens') or 0)}"
        )
    return "｜".join(parts)


def status_text(state: dict[str, Any]) -> str:
    last = state.get("last_decision")
    profile = str(state.get("execution_profile") or PROFILE_STABLE)
    light_profile = str(state.get("light_profile") or LIGHT_PROFILE_LUNA_STABLE)
    luna_mode = str(state.get("luna_mode") or LUNA_DISABLED)
    economics_policy = str(state.get("economics_policy") or DEFAULT_ECONOMICS_POLICY)
    if isinstance(last, dict):
        last_role = str(last.get("role") or "")
        if last.get("decision") == "TOOL_ONLY":
            last_text = "确定性工具 fast path"
        elif last.get("decision") == "DELEGATE":
            last_text = recommended_executor_label(last_role, profile, light_profile, luna_mode)
        else:
            last_text = "Sol"
    else:
        last_text = "暂无"
    _, installed, wrapper_ready, parked = environment_details()
    ready = installed == len(ROLES) and wrapper_ready and not parked
    mode_labels = {"OFF": "已关闭", "SHADOW": "影子模式", "ON": "已开启"}
    profile_text = "GLM_FIRST" if profile == PROFILE_GLM_FIRST else "STABLE"
    # LUNA_STABLE is an internal legacy enum name for "Local not preferred";
    # the user-facing label must not suggest Luna is enabled.
    light_profile_text = "开启" if light_profile == LIGHT_PROFILE_LOCAL_TEXT_FIRST else "关闭"
    if ready:
        environment = "就绪"
    elif parked:
        environment = "已停用（可用安装器 --enable 恢复）"
    else:
        environment = f"未就绪（agent {installed}/{len(ROLES)}，wrapper {'已安装' if wrapper_ready else '缺失'}）"
    counts = state.get("execution_counts") or {}
    completed = int(counts.get("completed", 0))
    failed = int(counts.get("failed", 0))
    actual = last_execution_text(state.get("last_execution"))
    writer = "忙碌" if isinstance(state.get("active_writer"), dict) else "空闲"
    if luna_mode == LUNA_BOUNDED:
        luna_status = "Luna 已开启（仅低风险 bounded 轻任务，复杂/多模态仍走 GLM/Terra/Sol）"
    else:
        luna_status = "Luna 已关闭（默认；$router-control luna 开启 后仅承接 bounded 轻任务）"
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
            local_status = f"本地文本模型未就绪（{local_reason}），scout 改用后备链"
        elif local_config.env_key and not local_provider_key(local_config):
            local_status = f"{local_config.display_name} Key 未配置，scout 改用后备链"
        else:
            local_health = read_local_health()
            if local_health.get("state") == "closed":
                local_status = f"{local_config.display_name} 可用（只读轻任务）"
            else:
                local_status = f"{local_config.display_name} 熔断，scout 改用后备链"
    else:
        local_status = "本地文本模型未启用"
    return (
        f"智能路由：{mode_labels[state['mode']]}（仅当前会话）｜重任务：{profile_text}｜Local：{light_profile_text}｜"
        f"经济门：{economics_policy}｜{luna_status}｜"
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
        hook_context(
            "SessionStart",
            f"SR_SESSION mode={state['mode']} luna={state['luna_mode']} restored for this session.",
        )


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
    if action in {
        "ON", "SHADOW", "OFF",
        "GLM_ON", "GLM_OFF", "LOCAL_ON", "LOCAL_OFF", "LUNA_ON", "LUNA_OFF",
        "ECON_V1", "ECON_V2",
    }:
        if action in {"ON", "GLM_ON", "LOCAL_ON", "LUNA_ON"}:
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
        elif action == "LUNA_ON":
            state = set_luna_mode(root, sid, LUNA_BOUNDED, activate=True)
        elif action == "LUNA_OFF":
            state = set_luna_mode(root, sid, LUNA_DISABLED)
        elif action == "ECON_V1":
            state = set_economics_policy(root, sid, "V1_COMPAT")
        elif action == "ECON_V2":
            state = set_economics_policy(root, sid, "V2_STATIC")
        else:
            state = set_mode(root, sid, action)
        append_telemetry(
            root,
            {
                "event": "routing_control_changed",
                "mode": state["mode"],
                "execution_profile": state["execution_profile"],
                "light_profile": state["light_profile"],
                "luna_mode": state["luna_mode"],
                "economics_policy": state["economics_policy"],
                "session": state["session_key"][:12],
            },
        )
        replies = {
            "ON": "已开启智能路由（仅当前会话）。Luna 默认关闭；边界清晰且适合委派的轻任务按 Local/GLM/Terra 后备链执行，高风险或不确定任务仍由 Sol 处理。",
            "SHADOW": "已开启影子模式（仅当前会话）。后续只显示路由预览，不会实际委派。",
            "OFF": "已关闭智能路由（仅当前会话）。后续任务全部由 Sol 处理。",
            "GLM_ON": "已开启 GLM_FIRST 智能路由（仅当前会话）。复杂纯文本 worker/reviewer 优先 GLM-5.3 Max；Luna 关闭时轻任务也可按链使用 GLM；工作日 14:00–18:00、额度熔断或多模态时自动改用 Terra。",
            "GLM_OFF": "已关闭当前会话的 GLM_FIRST，恢复 STABLE 执行配置；智能路由的 ON/OFF 状态不变。",
            "LOCAL_ON": "已开启本地文本首选 LOCAL_TEXT_FIRST（仅当前会话）。批量只读侦察优先使用已配置的本地文本模型，失败或不可用时按后备链改用其他执行器；等待任务始终使用无模型的确定性长等待。",
            "LOCAL_OFF": "已关闭本地文本首选（仅当前会话）；智能路由的 ON/OFF 状态不变。Luna 开关不受影响。",
            "LUNA_ON": "已开启 Luna 并启用智能路由（仅当前会话）。Luna 仅承接低风险、边界明确的 scout/tester/docs 轻任务；架构设计、复杂跨模块归因、安全审查、生产决策、高风险写入和多模态审查仍由 Sol/Terra 处理。",
            "LUNA_OFF": "已关闭 Luna（仅当前会话）；智能路由的 ON/OFF 状态不变。Luna 不再作为首选或隐藏回退执行器。",
            "ECON_V1": "已切换为 V1_COMPAT 兼容经济门；恢复 v0.4.1 的 work_units 路由行为，ON/OFF 状态不变。",
            "ECON_V2": "已切换为 V2_STATIC 保守经济门；微任务和确定性查询不启动子模型，只有足够大的独立工作包才委派，ON/OFF 状态不变。",
        }
        hook_context("UserPromptSubmit", f'SMART_ROUTER_UI_REPLY: 请原样回复："{replies[action]}"')
        return
    if action in {"STATUS", "HELP"}:
        if action == "HELP":
            reply = (
                "当前会话可用命令：$router-control 开启、glm 开启、glm 关闭、local 开启、local 关闭、"
                "luna 开启、luna 关闭、影子模式、关闭、状态。"
                "经济策略 v2 使用保守门，经济策略 v1 恢复兼容门；开启后自动判断；影子模式只预览；全局关闭请使用 /plugins。"
            )
        else:
            reply = status_text(state)
        hook_context("UserPromptSubmit", f'SMART_ROUTER_UI_REPLY: 请原样回复："{reply}"')
        return
    if state["mode"] == "OFF":
        return
    decision = classify(
        prompt,
        economics=True,
        economics_policy=state["economics_policy"],
        execution_profile=state["execution_profile"],
        light_profile=state["light_profile"],
    )
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
            "telemetry_schema_version": 2,
            "policy_version": "v0.4.3-alpha",
            "mode": state["mode"],
            "decision": decision["decision"],
            "role": decision.get("role"),
            "execution_profile": state["execution_profile"],
            "light_profile": state["light_profile"],
            "luna_mode": state["luna_mode"],
            "economics_policy": state["economics_policy"],
            "risk": decision["risk"],
            "reason_codes": decision["reason_codes"],
            "estimated_work_units": decision.get("estimated_work_units"),
            "estimated_parent_review_ratio": decision.get("estimated_parent_review_ratio"),
            "task_bucket": decision.get("task_bucket"),
            "gate_features": decision.get("gate_features", {}),
            "cost_estimate_status": decision.get("cost_estimate_status"),
            "parent_verification_observability": "unavailable_in_current_hook_api",
            "cost_dimensions": {
                "quality": "hard_rules_only",
                "sol_quota": "unavailable",
                "total_tokens": "child_only_after_execution",
                "latency": "child_only_after_execution",
            },
            "prompt_sha256": decision_id,
            "session": state["session_key"][:12],
        },
    )
    hook_context(
        "UserPromptSubmit",
        routing_context(
            state["mode"],
            decision,
            state["execution_profile"],
            state["light_profile"],
            state["luna_mode"],
        ),
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
        # Router ON denies every native subagent (explorer/worker/reviewer and
        # external plugin agents): a native child inherits Sol, bypasses
        # route_task, receipt v2, the single-slot lease, and provider telemetry.
        # Nothing here claims the delegation slot or synthesizes write leases.
        root = data_root()
        sid = session_id(payload)
        state = load_state(root, sid)
        if state["mode"] != "ON":
            return
        decision = state.get("last_decision") or {}
        decision_state = str(decision.get("decision") or "")
        inline_reason = (
            "Smart Router is ON without a usable DELEGATE decision (for example automatic Goal continuation, "
            "which does not raise UserPromptSubmit); native subagents are denied. Handle this work inline in Sol. "
            "If you truly need a native subagent, turn Smart Router off first."
        )
        if decision_state == "DELEGATE":
            delegation = state.get("current_delegation") or {}
            slot_state = ""
            if (
                delegation.get("decision_id") == decision.get("decision_id")
                and delegation.get("lease_id") == decision.get("lease_id")
            ):
                slot_state = str(delegation.get("status") or "")
            if slot_state == "available":
                reason = (
                    "Smart Router is ON and this objective holds an unconsumed DELEGATE decision; native subagents "
                    "are denied. Call synchronous smart_router.route_task once with the current decision_id and "
                    "lease_id instead."
                )
            elif slot_state in {"started", "running"}:
                reason = (
                    "A synchronous routed task is already running for this objective; wait for its receipt to "
                    "resume this Sol turn. Do not spawn a native agent or replay the consumed lease."
                )
            elif slot_state in {"completed", "failed"}:
                reason = (
                    "This objective's single delegation already finished; integrate the existing receipt instead "
                    "of delegating or spawning again."
                )
            else:
                reason = inline_reason
        elif decision_state == "TOOL_ONLY":
            reason = (
                "This objective is a deterministic TOOL_ONLY fast path; use the minimum direct tool call in Sol, "
                "not a child agent."
            )
        else:
            # Covers INLINE_SOL and restored sessions with no last_decision,
            # including automatic Goal continuation: Codex raises no
            # UserPromptSubmit for it, so no fresh decision or lease exists and
            # an old lease must never be reused.
            reason = inline_reason
        append_telemetry(
            root,
            {
                "event": "native_spawn_denied",
                "role": role,
                "decision": decision_state or None,
                "session": state["session_key"][:12],
            },
        )
        deny_pretool(reason)
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
    requested_luna_mode = str(tool_input.get("luna_mode") or LUNA_DISABLED).upper()
    if is_wrapper and requested_luna_mode != state.get("luna_mode", LUNA_DISABLED):
        deny_pretool("Requested luna mode does not match the current session luna mode.")
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
    if is_wrapper and (decision.get("gate_features") or {}).get("semantic_multimodal") and not images:
        deny_pretool("Semantic multimodal routing requires the task's attached image paths so provider selection can force Terra.")
        return
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
        "selected_executor": receipt_meta.get("selected_executor"),
        "attempted_executors": receipt_meta.get("attempted_executors"),
        "final_executor": receipt_meta.get("final_executor"),
        "route_path": receipt_meta.get("route_path"),
        "route_path_label": receipt_meta.get("route_path_label"),
        "fallback_occurred": receipt_meta.get("fallback_occurred"),
        "fallback_stage": receipt_meta.get("fallback_stage"),
        "fallback_reason_code": receipt_meta.get("fallback_reason_code"),
        "fallback_reason": receipt_meta.get("fallback_reason"),
        "selection_bypass_reason": receipt_meta.get("selection_bypass_reason"),
        "selection_bypass_reasons": receipt_meta.get("selection_bypass_reasons"),
        "usage": receipt_meta.get("usage"),
        "attempt_usage": receipt_meta.get("attempt_usage"),
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
            "selected_executor": receipt_meta.get("selected_executor"),
            "attempted_executors": receipt_meta.get("attempted_executors"),
            "final_executor": receipt_meta.get("final_executor"),
            "route_path": receipt_meta.get("route_path"),
            "route_path_label": receipt_meta.get("route_path_label"),
            "fallback_occurred": receipt_meta.get("fallback_occurred"),
            "fallback_stage": receipt_meta.get("fallback_stage"),
            "fallback_reason_code": receipt_meta.get("fallback_reason_code"),
            "selection_bypass_reason": receipt_meta.get("selection_bypass_reason"),
            "selection_bypass_reasons": receipt_meta.get("selection_bypass_reasons"),
            "usage": receipt_meta.get("usage"),
            "usage_stream_kind": receipt_meta.get("usage_stream_kind"),
            "usage_counter_semantics": receipt_meta.get("usage_counter_semantics"),
            "usage_adapter_version": receipt_meta.get("usage_adapter_version"),
            "attempt_usage": receipt_meta.get("attempt_usage"),
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
