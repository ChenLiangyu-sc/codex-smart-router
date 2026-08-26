#!/usr/bin/env python3
"""Local MCP server exposing the pinned Smart Router wrapper as one typed tool."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from provider_policy import (
    EXECUTION_PROFILES,
    LIGHT_PROFILES,
    LIGHT_PROFILE_LUNA_STABLE,
    LUNA_DISABLED,
    LUNA_MODES,
    PROFILE_STABLE,
)
from run_agent import ROLE_SETTINGS, RoutedTaskFailure, run_task
from router_core import consume_runtime_lease, data_root, delegation_task_digest

_REPLY_LOCK = threading.Lock()
_CANCEL_LOCK = threading.Lock()
_CANCEL_EVENTS: dict[Any, threading.Event] = {}


def routing_enabled() -> bool:
    configured = os.environ.get("CODEX_HOME")
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    if (codex_home / "smart-router" / "DISABLED").exists():
        return False
    try:
        text = (codex_home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return False
    pattern = re.compile(
        r'^\[plugins\."codex-smart-router@[^"\]]+"\]\s*$'
        r'(.*?)(?=^\[|\Z)',
        re.M | re.S,
    )
    for match in pattern.finditer(text):
        if re.search(r"^\s*enabled\s*=\s*true\s*$", match.group(1), re.M | re.I):
            return True
    return False


def plugin_version() -> str:
    try:
        path = __import__("pathlib").Path(__file__).resolve().parents[1] / ".codex-plugin" / "plugin.json"
        return str(json.loads(path.read_text(encoding="utf-8"))["version"])
    except (OSError, KeyError, json.JSONDecodeError, TypeError):
        return "unknown"


def reply(request_id: Any, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    payload["error" if error is not None else "result"] = error if error is not None else (result or {})
    with _REPLY_LOCK:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def tool_definition() -> dict[str, Any]:
    return {
        "name": "route_task",
        "title": "Run one bounded routed task",
        "description": (
            "Run one low-risk task with the exact SR_ON role, profiles, and luna mode. "
            "Luna is opt-in: with LUNA_DISABLED it never executes, and light roles follow the "
            "Local/GLM/Terra chain; LUNA_BOUNDED only admits low-risk bounded scout/tester/docs tasks. "
            "GLM_FIRST selects GLM-5.3 for eligible text work and automatically uses Terra for peak "
            "windows, provider fallback, or attached images. At most two models actually execute."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision_id", "lease_id", "role", "task"],
            "properties": {
                "decision_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "lease_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "role": {
                    "type": "string",
                    "enum": sorted(set(ROLE_SETTINGS) - {"router_monitor"}),
                },
                "task": {"type": "string", "minLength": 1, "maxLength": 20000},
                "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600, "default": 900},
                "execution_profile": {
                    "type": "string",
                    "enum": sorted(EXECUTION_PROFILES),
                    "default": PROFILE_STABLE,
                },
                "light_profile": {
                    "type": "string",
                    "enum": sorted(LIGHT_PROFILES),
                    "default": LIGHT_PROFILE_LUNA_STABLE,
                },
                "luna_mode": {
                    "type": "string",
                    "enum": sorted(LUNA_MODES),
                    "default": LUNA_DISABLED,
                },
                "images": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "description": "Local image paths needed by the task. Any image forces a multimodal Terra executor.",
                },
            },
        },
    }


def wait_tool_definition() -> dict[str, Any]:
    return {
        "name": "wait_for_condition",
        "title": "Wait deterministically for one condition",
        "description": (
            "Block once without invoking a model or polling from the parent. Wait for a process to exit, a file to "
            "appear/disappear, or exact text to appear in a file. Returns when satisfied, timed out, or cancelled."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision_id", "lease_id", "condition", "target"],
            "properties": {
                "decision_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "lease_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "condition": {
                    "type": "string",
                    "enum": ["process_exit", "file_exists", "file_absent", "file_contains"],
                },
                "target": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "description": "PID for process_exit; otherwise a local file path.",
                },
                "expected": {"type": "string", "minLength": 1, "maxLength": 1000},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 900},
                "interval_seconds": {"type": "number", "minimum": 0.2, "maximum": 30, "default": 2},
            },
        },
    }


def _condition_observed(condition: str, target: str, expected: str | None) -> tuple[bool, str]:
    if condition == "process_exit":
        if not re.fullmatch(r"[1-9][0-9]{0,9}", target):
            raise ValueError("process_exit target must be a positive PID")
        try:
            os.kill(int(target), 0)
        except ProcessLookupError:
            return True, "process is no longer running"
        except PermissionError:
            return False, "process still exists (permission denied for signal probe)"
        return False, "process is still running"
    path = Path(target).expanduser()
    if condition == "file_exists":
        return path.exists(), "file exists" if path.exists() else "file does not exist"
    if condition == "file_absent":
        return not path.exists(), "file is absent" if not path.exists() else "file still exists"
    if condition == "file_contains":
        if not expected:
            raise ValueError("file_contains requires expected text")
        if not path.is_file():
            return False, "file does not exist"
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("file_contains refuses files larger than 32 MiB")
        content = path.read_text(encoding="utf-8", errors="replace")
        return expected in content, "expected text found" if expected in content else "expected text not found"
    raise ValueError(f"unsupported condition: {condition}")


def wait_for_condition(
    args: dict[str, Any], cancel_event: threading.Event | None = None
) -> dict[str, Any]:
    condition = str(args.get("condition") or "")
    target = str(args.get("target") or "")
    expected = args.get("expected")
    if expected is not None and not isinstance(expected, str):
        raise ValueError("expected must be text")
    timeout = int(args.get("timeout_seconds", 900))
    interval = float(args.get("interval_seconds", 2))
    if not 1 <= timeout <= 3600 or not 0.2 <= interval <= 30:
        raise ValueError("wait timeout or interval is outside the supported range")
    started = time.monotonic()
    observed = "condition not checked"
    while True:
        if cancel_event is not None and cancel_event.is_set():
            status = "cancelled"
            observed = "wait cancelled by the MCP client"
            break
        satisfied, observed = _condition_observed(condition, target, expected)
        elapsed = time.monotonic() - started
        if satisfied:
            status = "completed"
            break
        if elapsed >= timeout:
            status = "timeout"
            break
        delay = min(interval, max(0.0, timeout - elapsed))
        if cancel_event is None:
            time.sleep(delay)
        elif cancel_event.wait(delay):
            status = "cancelled"
            observed = "wait cancelled by the MCP client"
            break
    total_elapsed = time.monotonic() - started
    return {
        "status": status,
        "condition": condition,
        "observed": observed,
        "elapsed_seconds": round(total_elapsed, 3),
        "_router_meta": {
            "role": "router_monitor",
            "model": None,
            "provider": "deterministic",
            "route_label": "确定性长等待（无模型轮询）",
            "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
            "duration_ms": max(0, int(total_elapsed * 1000)),
        },
    }


def handle(message: dict[str, Any], cancel_event: threading.Event | None = None) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        params = message.get("params") or {}
        reply(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "codex-smart-router", "version": plugin_version()},
            },
        )
    elif method == "ping":
        reply(request_id, {})
    elif method == "tools/list":
        reply(request_id, {"tools": [tool_definition(), wait_tool_definition()]})
    elif method == "tools/call":
        params = message.get("params") or {}
        tool_name = params.get("name")
        if tool_name not in {"route_task", "wait_for_condition"}:
            reply(request_id, error={"code": -32602, "message": "unknown tool"})
            return
        args = params.get("arguments") or {}
        try:
            if not routing_enabled():
                raise PermissionError("Smart Router is globally disabled or locally parked")
            decision_id = str(args.get("decision_id") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", decision_id):
                raise ValueError("decision_id must be a lowercase SHA-256 hex digest")
            lease_id = str(args.get("lease_id") or "")
            if not re.fullmatch(r"[0-9a-f]{32}", lease_id):
                raise ValueError("lease_id must be a lowercase 128-bit hex nonce")
            role = "router_monitor" if tool_name == "wait_for_condition" else str(args.get("role") or "")
            task_digest = delegation_task_digest(tool_name, args)
            if not consume_runtime_lease(data_root(), decision_id, lease_id, role, task_digest):
                raise PermissionError("no matching unconsumed routing lease for this task")
            if tool_name == "wait_for_condition":
                result = wait_for_condition(args, cancel_event)
                reply(
                    request_id,
                    {
                        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                        "structuredContent": result,
                        "isError": result["status"] in {"failed", "timeout", "cancelled"},
                    },
                )
                return
            if str(args.get("role") or "") == "router_monitor":
                raise ValueError("router_monitor must use wait_for_condition; model polling is disabled")
            profile = str(args.get("execution_profile") or PROFILE_STABLE).upper()
            light_profile = str(args.get("light_profile") or LIGHT_PROFILE_LUNA_STABLE).upper()
            luna_mode = str(args.get("luna_mode") or LUNA_DISABLED).upper()
            images = args.get("images") or []
            if not isinstance(images, list) or not all(isinstance(item, str) for item in images):
                raise ValueError("images must be an array of local path strings")
            receipt = run_task(
                str(args.get("role") or ""),
                str(args.get("task") or ""),
                execution_profile=profile,
                light_profile=light_profile,
                luna_mode=luna_mode,
                images=images,
                timeout=int(args.get("timeout_seconds", 900)),
                objective_id=decision_id,
            )
            failed = receipt.get("status") in {"blocked", "failed"}
            reply(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(receipt, ensure_ascii=False)}],
                    "structuredContent": receipt,
                    "isError": failed,
                },
            )
        except RoutedTaskFailure as exc:
            # Every planned executor failed: still return the structured
            # fallback ledger so hooks, telemetry, and the status page record
            # the same route path a successful run would have shown.
            failure = {"status": "failed", "_router_meta": exc.router_meta}
            reply(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(failure, ensure_ascii=False)}],
                    "structuredContent": failure,
                    "isError": True,
                },
            )
        except Exception as exc:
            message_text = f"{type(exc).__name__}: {exc}"
            reply(
                request_id,
                {"content": [{"type": "text", "text": message_text}], "isError": True},
            )
    elif request_id is not None:
        reply(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> int:
    threads: list[threading.Thread] = []
    for line in sys.stdin:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                if value.get("method") == "notifications/cancelled":
                    request_id = (value.get("params") or {}).get("requestId")
                    with _CANCEL_LOCK:
                        event = _CANCEL_EVENTS.get(request_id)
                    if event is not None:
                        event.set()
                    continue
                if value.get("method") == "tools/call" and value.get("id") is not None:
                    request_id = value["id"]
                    event = threading.Event()
                    with _CANCEL_LOCK:
                        _CANCEL_EVENTS[request_id] = event

                    def run_tool(message: dict[str, Any] = value, request: Any = request_id, stop: threading.Event = event) -> None:
                        try:
                            handle(message, stop)
                        finally:
                            with _CANCEL_LOCK:
                                _CANCEL_EVENTS.pop(request, None)

                    thread = threading.Thread(target=run_tool, daemon=False)
                    thread.start()
                    threads.append(thread)
                else:
                    handle(value)
        except Exception as exc:
            print(f"smart-router MCP warning: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    for thread in threads:
        thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
