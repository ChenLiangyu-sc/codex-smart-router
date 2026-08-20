#!/usr/bin/env python3
"""Run a pinned Terra/Luna Codex worker with a strict receipt schema."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from router_core import (
    ROLES,
    WRITER_ROLES,
    classify,
    validate_receipt,
    workspace_writer_lock,
    write_authorized_for,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = PLUGIN_ROOT / "assets" / "receipt.schema.json"
AGENTS = PLUGIN_ROOT / "install" / "agent-definitions"
ROLE_SETTINGS = {
    "router_scout": ("gpt-5.6-luna", "medium", "read-only"),
    "router_worker": ("gpt-5.6-terra", "medium", "workspace-write"),
    "router_reviewer": ("gpt-5.6-terra", "high", "read-only"),
    "router_monitor": ("gpt-5.6-luna", "low", "read-only"),
    "router_tester": ("gpt-5.6-luna", "medium", "workspace-write"),
    "router_docs": ("gpt-5.6-luna", "medium", "workspace-write"),
}


def role_instructions(role: str) -> str:
    raw = (AGENTS / f"{role}.toml").read_text(encoding="utf-8")
    match = re.search(r'developer_instructions\s*=\s*"""(.*?)"""', raw, re.S)
    if not match:
        raise ValueError(f"missing developer_instructions for {role}")
    return match.group(1).strip()


def build_command(role: str, output_path: Path) -> list[str]:
    model, effort, sandbox = ROLE_SETTINGS[role]
    return [
        "codex",
        "exec",
        "--ephemeral",
        "--disable",
        "hooks",
        "--disable",
        "multi_agent",
        "--skip-git-repo-check",
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{effort}"',
        "--config",
        f'sandbox_mode="{sandbox}"',
        "--config",
        "mcp_servers.smart_router.enabled=false",
        "--output-schema",
        str(SCHEMA),
        "--output-last-message",
        str(output_path),
        "-",
    ]


def run_task(
    role: str,
    task: str,
    timeout: int = 900,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    safety = classify(task)
    if safety["risk"] == "HIGH":
        raise ValueError("high-risk task must remain with the main Sol agent")
    if role in WRITER_ROLES and not write_authorized_for(task, role):
        raise ValueError("writable role requires explicit positive write authorization")
    prompt = (
        "You are a bounded Smart Router child process. Follow these role instructions exactly:\n\n"
        + role_instructions(role)
        + "\n\nAssigned task:\n"
        + task
        + "\n\nDo not spawn subagents. Return only the required receipt JSON. Keep summary within two sentences; "
        "include only decision-relevant evidence and no process narration. Keep each findings/evidence item under 700 "
        "characters, validation/remaining_risks item under 500, and changed_files path under 250. End every item "
        "at a complete sentence or path boundary. Never spill field names or continuation fragments into a new item."
    )
    target_workspace = Path(workspace) if workspace is not None else Path.cwd()

    def invoke() -> str:
        with tempfile.TemporaryDirectory(prefix="codex-smart-router-") as tmp:
            output = Path(tmp) / "receipt.json"
            result = subprocess.run(
                build_command(role, output),
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-2000:]
                raise RuntimeError(f"Codex child failed with exit {result.returncode}: {detail}")
            try:
                return output.read_text(encoding="utf-8")
            except FileNotFoundError as exc:
                raise RuntimeError("Codex child returned no receipt file") from exc

    if role in WRITER_ROLES:
        with workspace_writer_lock(target_workspace):
            raw = invoke()
    else:
        raw = invoke()
    valid, errors, receipt = validate_receipt(raw)
    if not valid or receipt is None:
        raise RuntimeError("invalid child receipt: " + "; ".join(errors))
    receipt["_router_meta"] = {
        "role": role,
        "model": ROLE_SETTINGS[role][0],
        "reasoning_effort": ROLE_SETTINGS[role][1],
        "executor": "codex-exec-wrapper",
    }
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=sorted(ROLES))
    parser.add_argument("--task", help="Bounded task; stdin is used when omitted")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    task = args.task if args.task is not None else __import__("sys").stdin.read()
    try:
        receipt = run_task(args.role, task, args.timeout)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
