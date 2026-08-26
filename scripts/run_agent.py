#!/usr/bin/env python3
"""Run a bounded Codex worker with provider-aware fallback and strict receipts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from provider_policy import (
    EXECUTION_PROFILES,
    EXECUTORS,
    GLM_ENV_KEY,
    GLM_PROVIDER_ID,
    LIGHT_PROFILES,
    LIGHT_PROFILE_LOCAL_TEXT_FIRST,
    LIGHT_PROFILE_LUNA_STABLE,
    LOCAL_TEXT_ELIGIBLE_ROLES,
    PROFILE_STABLE,
    ExecutorSpec,
    glm_key,
    record_glm_failure,
    record_glm_success,
    redact_secrets,
    resolve_executor,
)
from local_provider import (
    config_fingerprint as local_config_fingerprint,
    load_config as load_local_config,
    provider_key as local_provider_key,
    record_failure as record_local_failure,
    record_success as record_local_success,
)
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
GLM_CATALOG = PLUGIN_ROOT / "assets" / "glm-models.json"
AGENTS = PLUGIN_ROOT / "install" / "agent-definitions"

# Compatibility view for callers that only need the stable role mapping.
ROLE_SETTINGS = {
    "router_scout": ("gpt-5.6-luna", "medium", "read-only"),
    "router_worker": ("gpt-5.6-terra", "medium", "workspace-write"),
    "router_reviewer": ("gpt-5.6-terra", "high", "read-only"),
    "router_monitor": ("gpt-5.6-luna", "low", "read-only"),
    "router_tester": ("gpt-5.6-luna", "medium", "workspace-write"),
    "router_docs": ("gpt-5.6-luna", "medium", "workspace-write"),
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
NON_TOOL_ITEM_TYPES = {"agent_message", "reasoning", "error", "todo_list"}


class ChildFailure(RuntimeError):
    def __init__(self, detail: str, *, may_have_mutated: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.may_have_mutated = may_have_mutated


def role_instructions(role: str) -> str:
    raw = (AGENTS / f"{role}.toml").read_text(encoding="utf-8")
    match = re.search(r'developer_instructions\s*=\s*"""(.*?)"""', raw, re.S)
    if not match:
        raise ValueError(f"missing developer_instructions for {role}")
    return match.group(1).strip()


def validate_images(images: list[str] | tuple[str, ...] | None) -> list[Path]:
    paths: list[Path] = []
    for value in images or []:
        if len(paths) >= 5:
            raise ValueError("at most five images may be attached")
        path = Path(value).expanduser()
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"image must be a regular non-symlink file: {value}")
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(f"unsupported image type: {path.suffix}")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError(f"image exceeds 20 MiB: {path.name}")
        paths.append(path.resolve())
    return paths


def build_command(
    role: str,
    output_path: Path,
    executor: ExecutorSpec | None = None,
    images: list[Path] | None = None,
    home: str | Path | None = None,
) -> list[str]:
    if executor is None:
        model, effort, sandbox = ROLE_SETTINGS[role]
        executor = ExecutorSpec("stable_compat", "openai", model, effort, sandbox, model)
    local_config, _ = load_local_config(home)
    if executor.provider not in {"openai", GLM_PROVIDER_ID}:
        if (
            local_config is None
            or local_config.provider_id != executor.provider
            or local_config_fingerprint(local_config) != executor.provider_fingerprint
        ):
            raise ChildFailure("local provider configuration changed before command construction")
    excluded_keys = {GLM_ENV_KEY}
    if local_config is not None and local_config.env_key:
        excluded_keys.add(local_config.env_key)
    if executor.env_key:
        excluded_keys.add(executor.env_key)
    exclude_config = json.dumps([f"^{re.escape(key)}$" for key in sorted(excluded_keys)])
    command = [
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--strict-config",
        "--disable",
        "hooks",
        "--disable",
        "multi_agent",
        "--disable",
        "plugins",
        "--disable",
        "remote_plugin",
        "--disable",
        "apps",
        "--disable",
        "recommended_plugins",
        "--disable",
        "skill_search",
        "--disable",
        "skill_mcp_dependency_install",
        "--disable",
        "workspace_dependencies",
        "--skip-git-repo-check",
        "--json",
        "--model",
        executor.model,
        "--config",
        f'model_reasoning_effort="{executor.reasoning_effort}"',
        "--config",
        f'sandbox_mode="{executor.sandbox}"',
        "--config",
        'shell_environment_policy.inherit="core"',
        "--config",
        "shell_environment_policy.ignore_default_excludes=false",
        "--config",
        f"shell_environment_policy.exclude={exclude_config}",
    ]
    if executor.provider != "openai":
        if not executor.provider_name or not executor.base_url:
            raise ValueError(f"custom provider metadata is incomplete for {executor.id}")
        catalog = str(GLM_CATALOG) if executor.provider == GLM_PROVIDER_ID else executor.model_catalog
        provider = executor.provider
        command.extend(
            [
                "--config",
                f"model_provider={json.dumps(provider)}",
                "--config",
                f"model_providers.{provider}.name={json.dumps(executor.provider_name, ensure_ascii=False)}",
                "--config",
                f"model_providers.{provider}.base_url={json.dumps(executor.base_url)}",
                "--config",
                f"model_providers.{provider}.wire_api={json.dumps(executor.wire_api)}",
            ]
        )
        if executor.env_key:
            command.extend(
                ["--config", f"model_providers.{provider}.env_key={json.dumps(executor.env_key)}"]
            )
        if catalog:
            command.extend(["--config", f"model_catalog_json={json.dumps(str(catalog))}"])
    for image in images or []:
        command.extend(["--image", str(image)])
    command.extend(
        [
            "--output-schema",
            str(SCHEMA),
            "--output-last-message",
            str(output_path),
            "-",
        ]
    )
    return command


def _child_env(
    executor: ExecutorSpec,
    source: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> tuple[dict[str, str], str | None]:
    environment = dict(os.environ)
    if source is not None:
        environment.update(source)
    local_config, _ = load_local_config(home)
    known_keys = {GLM_ENV_KEY}
    if local_config is not None and local_config.env_key:
        known_keys.add(local_config.env_key)
    key = glm_key(environment, home) if executor.provider == GLM_PROVIDER_ID else None
    if executor.provider != "openai" and executor.provider != GLM_PROVIDER_ID:
        if (
            local_config is None
            or local_config.provider_id != executor.provider
            or local_config_fingerprint(local_config) != executor.provider_fingerprint
        ):
            raise ChildFailure("local provider configuration changed before execution")
        key = local_provider_key(local_config, environment, home)
    if executor.provider == GLM_PROVIDER_ID:
        if not key:
            raise ChildFailure("GLM credential is unavailable")
        environment[GLM_ENV_KEY] = key
    elif executor.provider != "openai" and executor.env_key:
        if not key:
            raise ChildFailure("local provider credential is unavailable")
        environment[executor.env_key] = key
    else:
        for name in known_keys:
            environment.pop(name, None)
    return environment, key


def _stream_evidence(stdout: str) -> tuple[bool, bool]:
    terminal_observed = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"turn.failed", "turn.completed"}:
            terminal_observed = True
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type and item_type not in NON_TOOL_ITEM_TYPES:
                return terminal_observed, True
    return terminal_observed, False


def _stream_may_have_mutated(stdout: str) -> bool:
    return _stream_evidence(stdout)[1]


def _writer_failure_may_have_mutated(
    role: str,
    before: str | None,
    after: str | None,
    stdout: str,
) -> bool:
    if role not in WRITER_ROLES:
        return False
    terminal_observed, tool_activity = _stream_evidence(stdout)
    fingerprints_known = before is not None and after is not None
    return bool(not fingerprints_known or not terminal_observed or before != after or tool_activity)


def _git_fingerprint(workspace: Path) -> str | None:
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if root.returncode != 0:
            return None
        parts: list[bytes] = []
        for command in (
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            ["git", "diff", "--binary", "--no-ext-diff"],
            ["git", "diff", "--cached", "--binary", "--no-ext-diff"],
        ):
            result = subprocess.run(command, cwd=workspace, capture_output=True, timeout=30, check=False)
            if result.returncode != 0:
                return None
            parts.append(result.stdout)
        return hashlib.sha256(b"\0".join(parts)).hexdigest()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _invoke(
    role: str,
    task: str,
    executor: ExecutorSpec,
    target_workspace: Path,
    timeout: int,
    images: list[Path],
    env_source: Mapping[str, str] | None,
    home: str | Path | None,
) -> tuple[str, str | None]:
    prompt = (
        "You are a bounded Smart Router child process. Follow these role instructions exactly:\n\n"
        + role_instructions(role)
        + "\n\nAssigned task:\n"
        + task
        + "\n\nDo not spawn subagents. Return only the required receipt JSON. Keep summary within two sentences; "
        "include only decision-relevant evidence and no process narration. Use at most six findings and six evidence "
        "items, at most eight changed_files, and at most six validation/remaining_risks items. Keep each "
        "findings/evidence item under 700 "
        "characters, validation/remaining_risks item under 500, and changed_files path under 250. End every item "
        "at a complete sentence or path boundary. Never spill field names or continuation fragments into a new item."
    )
    before = _git_fingerprint(target_workspace) if role in WRITER_ROLES else None
    environment, key = _child_env(executor, env_source, home)
    with tempfile.TemporaryDirectory(prefix="codex-smart-router-") as tmp:
        output = Path(tmp) / "receipt.json"
        command = build_command(role, output, executor, images, home)
        try:
            result = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                cwd=target_workspace,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            detail = redact_secrets(f"Codex child timed out after {timeout}s", [key])
            raise ChildFailure(detail, may_have_mutated=role in WRITER_ROLES) from exc
        if result.returncode != 0:
            after = _git_fingerprint(target_workspace) if role in WRITER_ROLES else before
            detail = redact_secrets((result.stderr or result.stdout).strip()[-4000:], [key])
            raise ChildFailure(
                f"Codex child failed with exit {result.returncode}: {detail}",
                may_have_mutated=_writer_failure_may_have_mutated(role, before, after, result.stdout),
            )
        try:
            return output.read_text(encoding="utf-8"), key
        except FileNotFoundError as exc:
            after = _git_fingerprint(target_workspace) if role in WRITER_ROLES else before
            raise ChildFailure(
                "Codex child returned no receipt file",
                may_have_mutated=_writer_failure_may_have_mutated(role, before, after, result.stdout),
            ) from exc


def _fallback_executor(role: str) -> ExecutorSpec:
    return EXECUTORS["terra_worker" if role == "router_worker" else "terra_reviewer"]


def run_task(
    role: str,
    task: str,
    timeout: int = 900,
    workspace: str | Path | None = None,
    *,
    execution_profile: str = PROFILE_STABLE,
    light_profile: str = LIGHT_PROFILE_LUNA_STABLE,
    images: list[str] | tuple[str, ...] | None = None,
    now: dt.datetime | None = None,
    env: Mapping[str, str] | None = None,
    codex_home: str | Path | None = None,
) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    profile = execution_profile.upper()
    if profile not in EXECUTION_PROFILES:
        raise ValueError(f"unsupported execution profile: {execution_profile}")
    normalized_light = light_profile.upper()
    if normalized_light not in LIGHT_PROFILES:
        raise ValueError(f"unsupported light profile: {light_profile}")
    safety = classify(task)
    if safety["risk"] == "HIGH":
        raise ValueError("high-risk task must remain with the main Sol agent")
    if role in WRITER_ROLES and not write_authorized_for(task, role):
        raise ValueError("writable role requires explicit positive write authorization")
    image_paths = validate_images(images)
    if image_paths and role in {"router_scout", "router_monitor", "router_tester", "router_docs"}:
        raise ValueError("image inputs require a Terra-capable worker or reviewer role")
    target_workspace = (Path(workspace) if workspace is not None else Path.cwd()).expanduser().resolve()
    resolution = resolve_executor(
        role,
        profile,
        light_profile=normalized_light,
        has_images=bool(image_paths),
        now=now,
        env=env,
        home=codex_home,
    )
    attempts: list[str] = []
    fallback_reason = (
        resolution.reason
        if resolution.executor.provider != GLM_PROVIDER_ID and profile != PROFILE_STABLE and role not in {"router_scout", "router_monitor", "router_tester", "router_docs"}
        else None
    )
    if (
        normalized_light == LIGHT_PROFILE_LOCAL_TEXT_FIRST
        and role in LOCAL_TEXT_ELIGIBLE_ROLES
        and not resolution.local_available
    ):
        fallback_reason = resolution.reason

    def execute(executor: ExecutorSpec) -> tuple[str, str | None]:
        attempts.append(executor.id)
        return _invoke(role, task, executor, target_workspace, timeout, image_paths, env, codex_home)

    def execute_with_fallback() -> tuple[str, ExecutorSpec, str | None]:
        executor = resolution.executor
        try:
            raw, _ = execute(executor)
            if resolution.local_available:
                valid_local, local_errors, _ = validate_receipt(raw)
                if not valid_local:
                    raise ChildFailure("local provider returned an invalid receipt: " + "; ".join(local_errors))
            if executor.provider == GLM_PROVIDER_ID:
                record_glm_success(
                    codex_home,
                    int(now.timestamp()) if now is not None else None,
                    expected_generation=resolution.health_generation,
                )
            elif resolution.local_available and resolution.local_config is not None:
                record_local_success(
                    resolution.local_config,
                    codex_home,
                    int(now.timestamp()) if now is not None else None,
                    expected_generation=resolution.health_generation,
                )
            return raw, executor, fallback_reason
        except ChildFailure as exc:
            if resolution.local_available and resolution.local_config is not None:
                key = local_provider_key(resolution.local_config, env, codex_home)
                record_local_failure(
                    resolution.local_config,
                    exc.detail,
                    key,
                    codex_home,
                    int(now.timestamp()) if now is not None else None,
                    expected_generation=resolution.health_generation,
                )
                fallback = EXECUTORS["luna_scout" if role == "router_scout" else "luna_monitor"]
                raw, _ = execute(fallback)
                return raw, fallback, "local_runtime_failure"
            if executor.provider != GLM_PROVIDER_ID:
                raise
            key = glm_key(env, codex_home)
            record_glm_failure(exc.detail, key, codex_home, int(now.timestamp()) if now is not None else None)
            if role in WRITER_ROLES and exc.may_have_mutated:
                raise ChildFailure(
                    "GLM child failed after possible workspace mutation; automatic writer fallback was suppressed. "
                    + exc.detail,
                    may_have_mutated=True,
                ) from exc
            fallback = _fallback_executor(role)
            raw, _ = execute(fallback)
            return raw, fallback, "glm_runtime_failure"

    if role in WRITER_ROLES:
        with workspace_writer_lock(target_workspace):
            raw, executor, actual_fallback_reason = execute_with_fallback()
    else:
        raw, executor, actual_fallback_reason = execute_with_fallback()
    valid, errors, receipt = validate_receipt(raw)
    if not valid or receipt is None:
        raise RuntimeError("invalid child receipt: " + "; ".join(errors))
    receipt["_router_meta"] = {
        "role": role,
        "model": executor.model,
        "provider": executor.provider,
        "reasoning_effort": executor.reasoning_effort,
        "executor": executor.id,
        "route_label": executor.route_label,
        "requested_profile": profile,
        "requested_light_profile": normalized_light,
        "selection_reason": resolution.reason,
        "fallback_reason": actual_fallback_reason,
        "attempted_executors": attempts,
    }
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=sorted(ROLES))
    parser.add_argument("--task", help="Bounded task; stdin is used when omitted")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--profile", choices=sorted(EXECUTION_PROFILES), default=PROFILE_STABLE)
    parser.add_argument("--light-profile", choices=sorted(LIGHT_PROFILES), default=LIGHT_PROFILE_LUNA_STABLE)
    parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args()
    task = args.task if args.task is not None else __import__("sys").stdin.read()
    try:
        receipt = run_task(
            args.role,
            task,
            args.timeout,
            execution_profile=args.profile,
            light_profile=args.light_profile,
            images=args.image,
        )
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
