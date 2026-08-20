#!/usr/bin/env python3
"""Safely install, disable, enable, inspect, or uninstall bundled Codex agents."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PLUGIN_ROOT / "install" / "agent-definitions"
MANIFEST_VERSION = 1
CONFIG_BEGIN = "# BEGIN codex-smart-router"
CONFIG_END = "# END codex-smart-router"
MCP_BEGIN = "# BEGIN codex-smart-router-mcp"
MCP_END = "# END codex-smart-router-mcp"


def plugin_version() -> str:
    try:
        value = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return str(value["version"])
    except (OSError, KeyError, json.JSONDecodeError, TypeError):
        return "unknown"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
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


def set_parked(codex_home: Path, parked: bool) -> None:
    marker = codex_home / "smart-router" / "DISABLED"
    if parked:
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".DISABLED.", dir=marker.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("Smart Router parked by install_agents.py\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, marker)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
    elif marker.exists() and not marker.is_symlink():
        marker.unlink()


def paths(codex_home: Path) -> tuple[Path, Path, Path]:
    management = codex_home / "smart-router"
    return codex_home / "agents", management / "disabled", management / "installed.json"


def _section_bounds(text: str, section: str) -> tuple[int, int] | None:
    lines = text.splitlines(keepends=True)
    start = None
    offset = 0
    for line in lines:
        stripped = line.strip()
        if stripped == f"[{section}]":
            start = offset + len(line)
        elif start is not None and stripped.startswith("[") and stripped.endswith("]"):
            return start, offset
        offset += len(line)
    return (start, len(text)) if start is not None else None


def _role_config_lines(codex_home: Path) -> list[str]:
    lines = []
    for source in source_files():
        raw = source.read_text(encoding="utf-8")
        name_match = __import__("re").search(r'^name\s*=\s*"([^"]+)"', raw, __import__("re").M)
        desc_match = __import__("re").search(r'^description\s*=\s*"([^"]+)"', raw, __import__("re").M)
        if not name_match or not desc_match:
            raise ValueError(f"invalid agent definition: {source}")
        name = name_match.group(1)
        target = str((codex_home / "agents" / source.name).resolve())
        lines.append(
            f"{name} = {{ description = {json.dumps(desc_match.group(1))}, "
            f"config_file = {json.dumps(target)} }}"
        )
    return lines


def plan_config(text: str, codex_home: Path) -> tuple[str, str, str]:
    """Return updated text, exact managed fragment, and disposition."""
    if CONFIG_BEGIN in text or CONFIG_END in text:
        raise ValueError("existing Smart Router config markers are not owned by this installer state")
    role_lines = _role_config_lines(codex_home)
    bounds = _section_bounds(text, "agents")
    if bounds is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        fragment = (
            f"{prefix}\n{CONFIG_BEGIN}\n[agents]\nenabled = true\n"
            f"max_concurrent_threads_per_session = 3\n"
            + "\n".join(role_lines)
            + f"\n{CONFIG_END}\n"
        )
        return text + fragment, fragment, "append-agents-section"
    start, end = bounds
    section = text[start:end]
    enabled = re_search_setting(section, "enabled")
    maximum = re_search_setting(section, "max_concurrent_threads_per_session")
    if enabled not in (None, "true"):
        raise ValueError("existing [agents].enabled is not true")
    if maximum is not None:
        try:
            if int(maximum) < 1:
                raise ValueError
        except ValueError as exc:
            raise ValueError("existing [agents].max_concurrent_threads_per_session is invalid") from exc
    for source in source_files():
        role = source.stem
        if re_search_setting(section, role) is not None or f"[agents.{role}]" in text:
            raise ValueError(f"existing [agents].{role} declaration conflicts with managed role")
    missing = []
    if enabled is None:
        missing.append("enabled = true")
    if maximum is None:
        missing.append("max_concurrent_threads_per_session = 3")
    missing.extend(role_lines)
    prefix = "" if start == 0 or text[:start].endswith("\n") else "\n"
    fragment = prefix + CONFIG_BEGIN + "\n" + "\n".join(missing) + "\n" + CONFIG_END + "\n"
    return text[:start] + fragment + text[start:], fragment, "extend-agents-section"


def plan_mcp_config(text: str) -> tuple[str, str]:
    if MCP_BEGIN in text or MCP_END in text or "[mcp_servers.smart_router]" in text:
        raise ValueError("existing mcp_servers.smart_router declaration conflicts with managed wrapper")
    prefix = "" if not text or text.endswith("\n") else "\n"
    server = str((PLUGIN_ROOT / "scripts" / "router_mcp.py").resolve())
    fragment = (
        f"{prefix}\n{MCP_BEGIN}\n[mcp_servers.smart_router]\ncommand = \"python3\"\n"
        f"args = [{json.dumps(server)}]\n{MCP_END}\n"
    )
    return text + fragment, fragment


def re_search_setting(section: str, key: str) -> str | None:
    import re

    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*([^#\n]+)", section, re.M)
    return match.group(1).strip().lower() if match else None


def install_config(codex_home: Path, apply: bool, manifest: dict[str, Any]) -> tuple[int, list[str]]:
    config = codex_home / "config.toml"
    try:
        original = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    working = original
    previous = manifest.get("config")
    if isinstance(previous, dict) and previous.get("managed_fragment"):
        old_fragment = previous["managed_fragment"]
        if working.count(old_fragment) != 1:
            return 1, [f"CONFIG_CONFLICT {config}: previously managed fragment was modified; left untouched"]
        working = working.replace(old_fragment, "", 1)
    if isinstance(previous, dict) and previous.get("mcp_fragment"):
        old_mcp = previous["mcp_fragment"]
        if working.count(old_mcp) != 1:
            return 1, [f"CONFIG_CONFLICT {config}: previously managed MCP fragment was modified; left untouched"]
        working = working.replace(old_mcp, "", 1)
    try:
        updated, fragment, disposition = plan_config(working, codex_home)
        updated, mcp_fragment = plan_mcp_config(updated)
    except ValueError as exc:
        return 1, [f"CONFIG_CONFLICT {config}: {exc}; left untouched"]
    messages = [f"CONFIG {disposition}: {config}"]
    if updated == original:
        return 0, messages
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(config),
            tofile=str(config) + " (planned)",
        )
    ).rstrip()
    messages.append(diff)
    if not apply:
        return 0, messages
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = codex_home / "smart-router" / "backups" / f"config.toml.{timestamp}.bak"
    if config.exists():
        atomic_copy(config, backup)
    config.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".config.toml.", dir=config.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, config)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    manifest["config"] = {
        "managed_fragment": fragment,
        "mcp_fragment": mcp_fragment,
        "backup": str(backup) if backup.exists() else None,
        "status": "active",
    }
    return 0, messages


def uninstall_config(codex_home: Path, manifest: dict[str, Any]) -> tuple[int, list[str]]:
    entry = manifest.get("config")
    if not isinstance(entry, dict) or not entry.get("managed_fragment"):
        return 0, ["CONFIG KEEP: no router-managed config fragment"]
    config = codex_home / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        manifest.pop("config", None)
        return 0, ["CONFIG FORGET: config file already absent"]
    fragments = [entry["managed_fragment"]]
    if entry.get("mcp_fragment"):
        fragments.append(entry["mcp_fragment"])
    for fragment in fragments:
        if text.count(fragment) != 1:
            return 1, [f"CONFIG PRESERVE {config}: managed fragment missing or modified"]
    updated = text
    for fragment in fragments:
        updated = updated.replace(fragment, "", 1)
    fd, tmp_name = tempfile.mkstemp(prefix=".config.toml.", dir=config.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, config)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    manifest.pop("config", None)
    return 0, [f"CONFIG REMOVED managed fragment from {config}"]


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        value = {}
    if not isinstance(value.get("files"), dict):
        value = {"manifest_version": MANIFEST_VERSION, "files": {}}
    value["plugin_version"] = plugin_version()
    return value


def source_files() -> list[Path]:
    return sorted(SOURCE_DIR.glob("router_*.toml"))


def inspect(codex_home: Path) -> list[dict[str, str]]:
    agents, disabled, manifest_path = paths(codex_home)
    manifest = load_manifest(manifest_path)
    result = []
    for source in source_files():
        entry = manifest["files"].get(source.name, {})
        target = agents / source.name
        parked = disabled / source.name
        if target.exists():
            state = "active-matching" if digest(target) == digest(source) else "active-conflict"
        elif parked.exists():
            state = "disabled-matching" if digest(parked) == digest(source) else "disabled-modified"
        else:
            state = "missing"
        result.append({"file": source.name, "state": state, "owned": str(bool(entry)).lower()})
    return result


def install(codex_home: Path, apply: bool) -> tuple[int, list[str]]:
    agents, _, manifest_path = paths(codex_home)
    manifest = load_manifest(manifest_path)
    config_errors, planned_config_messages = install_config(codex_home, False, manifest)
    if config_errors:
        return config_errors, planned_config_messages
    messages: list[str] = []
    errors = 0
    for source in source_files():
        target = agents / source.name
        source_hash = digest(source)
        if target.exists() and digest(target) != source_hash:
            messages.append(f"CONFLICT {target}: existing file differs; left untouched")
            errors += 1
            continue
        action = "KEEP" if target.exists() else ("INSTALL" if apply else "WOULD_INSTALL")
        messages.append(f"{action} {target}")
        if apply and not target.exists():
            atomic_copy(source, target)
        if apply:
            manifest["files"][source.name] = {"sha256": source_hash, "status": "active"}
    if apply:
        config_errors, config_messages = install_config(codex_home, True, manifest)
    else:
        config_errors, config_messages = 0, planned_config_messages
    errors += config_errors
    messages.extend(config_messages)
    if apply and not config_errors:
        set_parked(codex_home, False)
        atomic_json(manifest_path, manifest)
    return errors, messages


def disable(codex_home: Path) -> tuple[int, list[str]]:
    agents, disabled, manifest_path = paths(codex_home)
    manifest = load_manifest(manifest_path)
    messages: list[str] = []
    errors = 0
    for name, entry in sorted(manifest["files"].items()):
        source = agents / name
        target = disabled / name
        expected = entry.get("sha256")
        if not source.exists():
            messages.append(f"SKIP {source}: not active")
            continue
        if digest(source) != expected:
            messages.append(f"PRESERVE {source}: modified since installation")
            errors += 1
            continue
        if target.exists():
            messages.append(f"CONFLICT {target}: disabled target already exists")
            errors += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        entry["status"] = "disabled"
        messages.append(f"DISABLED {name}")
    atomic_json(manifest_path, manifest)
    if not errors:
        set_parked(codex_home, True)
        messages.append("PARKED smart_router wrapper")
    return errors, messages


def enable(codex_home: Path) -> tuple[int, list[str]]:
    agents, disabled, manifest_path = paths(codex_home)
    manifest = load_manifest(manifest_path)
    messages: list[str] = []
    errors = 0
    for name, entry in sorted(manifest["files"].items()):
        source = disabled / name
        target = agents / name
        if entry.get("status") != "disabled":
            continue
        if not source.exists() or digest(source) != entry.get("sha256"):
            messages.append(f"PRESERVE {source}: missing or modified")
            errors += 1
            continue
        if target.exists():
            messages.append(f"CONFLICT {target}: active target already exists")
            errors += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        entry["status"] = "active"
        messages.append(f"ENABLED {name}")
    atomic_json(manifest_path, manifest)
    if not errors:
        set_parked(codex_home, False)
        messages.append("UNPARKED smart_router wrapper")
    return errors, messages


def uninstall(codex_home: Path) -> tuple[int, list[str]]:
    agents, disabled, manifest_path = paths(codex_home)
    manifest = load_manifest(manifest_path)
    messages: list[str] = []
    errors = 0
    remaining: dict[str, Any] = {}
    for name, entry in sorted(manifest["files"].items()):
        location = (disabled if entry.get("status") == "disabled" else agents) / name
        if not location.exists():
            messages.append(f"FORGET {name}: already absent")
            continue
        if digest(location) != entry.get("sha256"):
            messages.append(f"PRESERVE {location}: modified since installation")
            remaining[name] = entry
            errors += 1
            continue
        location.unlink()
        messages.append(f"REMOVED {location}")
    manifest["files"] = remaining
    config_errors, config_messages = uninstall_config(codex_home, manifest)
    errors += config_errors
    messages.extend(config_messages)
    if not errors:
        set_parked(codex_home, False)
    atomic_json(manifest_path, manifest)
    return errors, messages


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="Install agents; default is dry-run")
    group.add_argument("--disable", action="store_true", help="Recoverably park managed agents")
    group.add_argument("--enable", action="store_true", help="Restore parked agents")
    group.add_argument("--uninstall", action="store_true", help="Remove unchanged managed agents")
    group.add_argument("--status", action="store_true", help="Show installation state")
    args = parser.parse_args()
    home = args.codex_home.expanduser().resolve()
    if args.status:
        print(json.dumps(inspect(home), ensure_ascii=False, indent=2))
        return 0
    if args.disable:
        errors, messages = disable(home)
    elif args.enable:
        errors, messages = enable(home)
    elif args.uninstall:
        errors, messages = uninstall(home)
    else:
        errors, messages = install(home, args.apply)
    print("\n".join(messages))
    if not any((args.apply, args.disable, args.enable, args.uninstall)):
        print("Dry-run only. Re-run with --apply to install.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
