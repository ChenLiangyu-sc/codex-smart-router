#!/usr/bin/env python3
"""Read-only compatibility probe for the local Codex runtime."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from install_agents import default_codex_home, inspect
from local_provider import load_config as load_local_config, provider_key as local_provider_key, read_health as read_local_health
from provider_policy import glm_key, is_peak_window, key_fingerprint, load_policy, read_health


def command(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=15, check=False)
        return result.returncode, (result.stdout + result.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def main() -> int:
    version_code, version_text = command("codex", "--version")
    feature_code, feature_text = command("codex", "features", "list")
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
    version_ok = bool(match and tuple(map(int, match.groups())) >= (0, 148, 0))
    features = {}
    for name in ("hooks", "multi_agent", "plugins"):
        features[name] = bool(re.search(rf"^{name}\s+\S+\s+true$", feature_text, re.M))
    home = default_codex_home()
    try:
        config_text = (home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        config_text = ""
    configured_roles = [
        item["file"].removesuffix(".toml")
        for item in inspect(home)
        if f'{item["file"].removesuffix(".toml")} = {{' in config_text
    ]
    mcp_code, mcp_text = command("codex", "mcp", "get", "smart_router")
    key = glm_key(home=home)
    policy = load_policy(home)
    local_config, local_reason = load_local_config(home)
    payload = {
        "codex_version": version_text,
        "version_supported": version_code == 0 and version_ok,
        "features": features,
        "agents": inspect(home),
        "configured_roles": configured_roles,
        "wrapper_registered": mcp_code == 0 and "router_mcp.py" in mcp_text,
        "glm": {
            "credential_configured": bool(key),
            "credential_fingerprint": key_fingerprint(key),
            "peak_window_active": is_peak_window(policy=policy),
            "policy": {name: value for name, value in policy.items() if name != "invalid"},
            "policy_valid": not policy.get("invalid", False),
            "health": read_health(home),
        },
        "local_text": {
            "configured": local_config is not None,
            "configuration_status": local_reason,
            "display_name": local_config.display_name if local_config else None,
            "model": local_config.model if local_config else None,
            "surrogate": local_config.surrogate if local_config else None,
            "credential_required": bool(local_config and local_config.env_key),
            "credential_configured": bool(local_config and (not local_config.env_key or local_provider_key(local_config, home=home))),
            "health": read_local_health(home),
        },
        "plugin_data": os.environ.get("PLUGIN_DATA", "managed by Codex when installed"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    required = (
        payload["version_supported"]
        and all(features.values())
        and len(configured_roles) == 6
        and payload["wrapper_registered"]
    )
    return 0 if required else 1


if __name__ == "__main__":
    raise SystemExit(main())
