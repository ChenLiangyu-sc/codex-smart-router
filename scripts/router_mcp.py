#!/usr/bin/env python3
"""Local MCP server exposing the pinned Smart Router wrapper as one typed tool."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from run_agent import ROLE_SETTINGS, run_task


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
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def tool_definition() -> dict[str, Any]:
    return {
        "name": "route_task",
        "title": "Run one bounded Terra/Luna task",
        "description": "Run one low-risk task with the exact SR_ON role. High-risk or mismatched work is rejected.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["role", "task"],
            "properties": {
                "role": {"type": "string", "enum": sorted(ROLE_SETTINGS)},
                "task": {"type": "string", "minLength": 1, "maxLength": 20000},
            },
        },
    }


def handle(message: dict[str, Any]) -> None:
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
        reply(request_id, {"tools": [tool_definition()]})
    elif method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != "route_task":
            reply(request_id, error={"code": -32602, "message": "unknown tool"})
            return
        args = params.get("arguments") or {}
        try:
            if not routing_enabled():
                raise PermissionError("Smart Router is globally disabled or locally parked")
            receipt = run_task(str(args.get("role") or ""), str(args.get("task") or ""))
            failed = receipt.get("status") in {"blocked", "failed"}
            reply(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(receipt, ensure_ascii=False)}],
                    "structuredContent": receipt,
                    "isError": failed,
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
    for line in sys.stdin:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                handle(value)
        except Exception as exc:
            print(f"smart-router MCP warning: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
