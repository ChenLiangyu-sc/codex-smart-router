#!/usr/bin/env python3
"""Safely install, disable, enable, inspect, or uninstall bundled Codex agents."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
import shutil
import stat
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
RUNTIME_SUBDIR = Path("smart-router") / "runtime-current"
RUNTIME_RELEASES_SUBDIR = Path("smart-router") / "runtime-releases"
LEGACY_RUNTIME_SUBDIR = Path("smart-router") / "runtime"


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


def plan_mcp_config(text: str, codex_home: Path) -> tuple[str, str]:
    if MCP_BEGIN in text or MCP_END in text or "[mcp_servers.smart_router]" in text:
        raise ValueError("existing mcp_servers.smart_router declaration conflicts with managed wrapper")
    prefix = "" if not text or text.endswith("\n") else "\n"
    server = str(codex_home / RUNTIME_SUBDIR / "scripts" / "router_mcp.py")
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
        updated, mcp_fragment = plan_mcp_config(updated, codex_home)
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


def check_uninstall_config(codex_home: Path, manifest: dict[str, Any]) -> tuple[int, list[str]]:
    entry = manifest.get("config")
    if not isinstance(entry, dict) or not entry.get("managed_fragment"):
        return 0, []
    config = codex_home / "config.toml"
    if _lexists(config) and (config.is_symlink() or not config.is_file()):
        return 1, [f"CONFIG PRESERVE {config}: config is not a regular file"]
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0, []
    fragments = [entry["managed_fragment"]]
    if entry.get("mcp_fragment"):
        fragments.append(entry["mcp_fragment"])
    if any(text.count(fragment) != 1 for fragment in fragments):
        return 1, [f"CONFIG PRESERVE {config}: managed fragment missing or modified"]
    return 0, []


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        value = {}
    if not isinstance(value.get("files"), dict):
        value = {"manifest_version": MANIFEST_VERSION, "files": {}}
    value["plugin_version"] = plugin_version()
    return value


def runtime_sources() -> list[tuple[Path, Path]]:
    relative_files = [
        Path(".codex-plugin") / "plugin.json",
        Path("hooks") / "router_hook.py",
        *[path.relative_to(PLUGIN_ROOT) for path in sorted((PLUGIN_ROOT / "scripts").glob("*.py"))],
        *[path.relative_to(PLUGIN_ROOT) for path in sorted((PLUGIN_ROOT / "assets").glob("*.json"))],
        *[
            path.relative_to(PLUGIN_ROOT)
            for path in sorted((PLUGIN_ROOT / "install" / "agent-definitions").glob("*.toml"))
        ],
    ]
    return [(relative, PLUGIN_ROOT / relative) for relative in relative_files]


def runtime_release_id() -> str:
    fingerprint = hashlib.sha256()
    for relative, source in runtime_sources():
        fingerprint.update(relative.as_posix().encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(digest(source).encode("ascii"))
        fingerprint.update(b"\n")
    return fingerprint.hexdigest()[:20]


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _runtime_expected() -> dict[str, tuple[Path, str]]:
    return {relative.as_posix(): (source, digest(source)) for relative, source in runtime_sources()}


def _check_directory(path: Path, label: str) -> str | None:
    if not _lexists(path):
        return None
    if path.is_symlink() or not path.is_dir():
        return f"RUNTIME_CONFLICT {path}: {label} is not a real directory; left untouched"
    return None


def _check_release(release: Path, expected: dict[str, tuple[Path, str]]) -> list[str]:
    errors: list[str] = []
    root_error = _check_directory(release, "release root")
    if root_error:
        return [root_error]
    expected_names = set(expected)
    actual_names: set[str] = set()
    for candidate in release.rglob("*"):
        relative = candidate.relative_to(release).as_posix()
        if candidate.is_symlink():
            errors.append(f"RUNTIME_CONFLICT {candidate}: symlink inside release; left untouched")
        elif candidate.is_file():
            actual_names.add(relative)
        elif not candidate.is_dir():
            errors.append(f"RUNTIME_CONFLICT {candidate}: special file inside release; left untouched")
    for name, (_, expected_hash) in expected.items():
        target = release / name
        cursor = release
        unsafe_parent = None
        for part in Path(name).parts[:-1]:
            cursor = cursor / part
            if _lexists(cursor) and (cursor.is_symlink() or not cursor.is_dir()):
                unsafe_parent = cursor
                break
        if unsafe_parent is not None:
            errors.append(f"RUNTIME_CONFLICT {unsafe_parent}: unsafe release path component; left untouched")
            continue
        if not _lexists(target):
            errors.append(f"RUNTIME_CONFLICT {target}: release file is missing; left untouched")
        elif target.is_symlink() or not target.is_file():
            errors.append(f"RUNTIME_CONFLICT {target}: release target is not a regular file; left untouched")
        elif digest(target) != expected_hash:
            errors.append(f"RUNTIME_CONFLICT {target}: immutable release was modified; left untouched")
    for name in sorted(actual_names - expected_names):
        errors.append(f"RUNTIME_CONFLICT {release / name}: unexpected file inside release; left untouched")
    return errors


def _runtime_link_target(release_id: str) -> str:
    return (Path("runtime-releases") / release_id).as_posix()


def _valid_release_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 20 and all(character in "0123456789abcdef" for character in value)


def _cleanup_staging(staging: Path, releases: Path) -> None:
    if (
        staging.parent == releases
        and staging.name.startswith(".staging-")
        and _lexists(staging)
        and not staging.is_symlink()
        and staging.is_dir()
    ):
        shutil.rmtree(staging)


def _install_runtime_unlocked(codex_home: Path, apply: bool, manifest: dict[str, Any]) -> tuple[int, list[str]]:
    management = codex_home / "smart-router"
    releases = codex_home / RUNTIME_RELEASES_SUBDIR
    current = codex_home / RUNTIME_SUBDIR
    release_id = runtime_release_id()
    release = releases / release_id
    link_target = _runtime_link_target(release_id)
    expected = _runtime_expected()
    messages: list[str] = []

    for path, label in ((management, "management root"), (releases, "releases root")):
        error = _check_directory(path, label)
        if error:
            messages.append(error)
    if messages:
        return len(messages), messages
    previous = manifest.get("runtime_release")
    if _lexists(current):
        if not current.is_symlink():
            messages.append(f"RUNTIME_CONFLICT {current}: stable entry is not a symlink; left untouched")
        else:
            actual_target = os.readlink(current)
            owned_target = previous.get("link_target") if isinstance(previous, dict) else None
            if actual_target not in {link_target, owned_target}:
                messages.append(f"RUNTIME_CONFLICT {current}: unowned symlink target; left untouched")
    if _lexists(release):
        messages.extend(_check_release(release, expected))
    if messages:
        return len(messages), messages

    if not apply:
        action = "RUNTIME_KEEP" if _lexists(current) and os.readlink(current) == link_target else "RUNTIME_WOULD_SWITCH"
        return 0, [f"{action} {current} -> {link_target}"]

    management.mkdir(parents=True, exist_ok=True, mode=0o700)
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _lexists(release):
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=releases))
        try:
            for name, (source, _) in expected.items():
                atomic_copy(source, staging / name)
            staging_errors = _check_release(staging, expected)
            if staging_errors:
                return len(staging_errors), staging_errors
            try:
                os.rename(staging, release)
            except FileExistsError:
                concurrent_errors = _check_release(release, expected)
                if concurrent_errors:
                    return len(concurrent_errors), concurrent_errors
        finally:
            _cleanup_staging(staging, releases)
    if not _lexists(current) or os.readlink(current) != link_target:
        fd, tmp_name = tempfile.mkstemp(prefix=".runtime-current.", dir=management)
        os.close(fd)
        os.unlink(tmp_name)
        try:
            os.symlink(link_target, tmp_name)
            os.replace(tmp_name, current)
        finally:
            if _lexists(Path(tmp_name)):
                os.unlink(tmp_name)
        action = "RUNTIME_SWITCHED"
    else:
        action = "RUNTIME_KEEP"
    if isinstance(manifest.get("runtime_files"), dict):
        manifest["legacy_runtime_files"] = manifest.pop("runtime_files")
    manifest["runtime_release"] = {
        "id": release_id,
        "link_target": link_target,
        "files": {name: {"sha256": source_hash} for name, (_, source_hash) in expected.items()},
    }
    return 0, [f"{action} {current} -> {link_target}"]


def install_runtime(codex_home: Path, apply: bool, manifest: dict[str, Any]) -> tuple[int, list[str]]:
    if not apply:
        return _install_runtime_unlocked(codex_home, apply, manifest)
    management = codex_home / "smart-router"
    error = _check_directory(management, "management root")
    if error:
        return 1, [error]
    management.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = management / "install.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        return 1, [f"RUNTIME_CONFLICT {lock_path}: cannot safely open install lock ({exc})"]
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            return 1, [f"RUNTIME_CONFLICT {lock_path}: install lock is not a regular file"]
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _install_runtime_unlocked(codex_home, apply, manifest)
    finally:
        os.close(lock_fd)


def check_uninstall_runtime(codex_home: Path, manifest: dict[str, Any]) -> tuple[int, list[str]]:
    current = codex_home / RUNTIME_SUBDIR
    release_entry = manifest.get("runtime_release")
    messages: list[str] = []
    management = codex_home / "smart-router"
    releases = codex_home / RUNTIME_RELEASES_SUBDIR
    for path, label in ((management, "management root"), (releases, "releases root")):
        error = _check_directory(path, label)
        if error:
            messages.append(error.replace("RUNTIME_CONFLICT", "RUNTIME_PRESERVE"))
    if messages:
        return len(messages), messages
    if isinstance(release_entry, dict):
        link_target = release_entry.get("link_target")
        release_id = release_entry.get("id")
        entries = release_entry.get("files")
        if (
            not _valid_release_id(release_id)
            or link_target != _runtime_link_target(release_id)
            or not isinstance(entries, dict)
        ):
            messages.append("RUNTIME_PRESERVE: invalid runtime release manifest")
        else:
            if _lexists(current) and (not current.is_symlink() or os.readlink(current) != link_target):
                messages.append(f"RUNTIME_PRESERVE {current}: stable entry was modified")
            release = codex_home / RUNTIME_RELEASES_SUBDIR / release_id
            if _lexists(release):
                expected: dict[str, tuple[Path, str]] = {}
                for name, entry in entries.items():
                    relative = Path(name)
                    if relative.is_absolute() or ".." in relative.parts or not isinstance(entry, dict):
                        messages.append(f"RUNTIME_PRESERVE {name}: invalid managed release path")
                        continue
                    expected[name] = (release / relative, str(entry.get("sha256", "")))
                messages.extend(
                    message.replace("RUNTIME_CONFLICT", "RUNTIME_PRESERVE")
                    for message in _check_release(release, expected)
                )
    legacy = manifest.get("legacy_runtime_files")
    if isinstance(legacy, dict):
        root = codex_home / LEGACY_RUNTIME_SUBDIR
        error = _check_directory(root, "legacy runtime root")
        if error:
            messages.append(error.replace("RUNTIME_CONFLICT", "RUNTIME_PRESERVE"))
        else:
            for name, entry in sorted(legacy.items()):
                relative = Path(name)
                target = root / relative
                if relative.is_absolute() or ".." in relative.parts or not isinstance(entry, dict):
                    messages.append(f"RUNTIME_PRESERVE {name}: invalid legacy managed path")
                    continue
                cursor = root
                unsafe_parent = None
                for part in relative.parts[:-1]:
                    cursor = cursor / part
                    if _lexists(cursor) and (cursor.is_symlink() or not cursor.is_dir()):
                        unsafe_parent = cursor
                        break
                if unsafe_parent is not None:
                    messages.append(f"RUNTIME_PRESERVE {unsafe_parent}: unsafe legacy path component")
                elif _lexists(target) and (
                    target.is_symlink() or not target.is_file() or digest(target) != entry.get("sha256")
                ):
                    messages.append(f"RUNTIME_PRESERVE {target}: modified since installation")
    return len(messages), messages


def _remove_empty_parents(paths_to_check: set[Path], stop: Path) -> None:
    for directory in sorted(paths_to_check, key=lambda path: len(path.parts), reverse=True):
        if directory == stop or stop not in directory.parents:
            continue
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


def uninstall_runtime(codex_home: Path, manifest: dict[str, Any]) -> tuple[int, list[str]]:
    errors, messages = check_uninstall_runtime(codex_home, manifest)
    if errors:
        return errors, messages
    current = codex_home / RUNTIME_SUBDIR
    release_entry = manifest.get("runtime_release")
    if isinstance(release_entry, dict):
        release = codex_home / RUNTIME_RELEASES_SUBDIR / release_entry["id"]
        parents: set[Path] = set()
        for name in sorted(release_entry["files"]):
            target = release / name
            if target.exists():
                target.unlink()
                messages.append(f"RUNTIME_REMOVED {target}")
                parents.update(target.parents)
        _remove_empty_parents(parents, codex_home / "smart-router")
        if _lexists(current):
            current.unlink()
            messages.append(f"RUNTIME_REMOVED {current}")
        manifest.pop("runtime_release", None)
    legacy = manifest.get("legacy_runtime_files")
    if isinstance(legacy, dict):
        root = codex_home / LEGACY_RUNTIME_SUBDIR
        parents: set[Path] = set()
        for name in sorted(legacy):
            target = root / name
            if target.exists():
                target.unlink()
                messages.append(f"RUNTIME_REMOVED {target}")
                parents.update(target.parents)
        _remove_empty_parents(parents, codex_home / "smart-router")
        manifest.pop("legacy_runtime_files", None)
    if not messages:
        messages.append("RUNTIME KEEP: no managed runtime files")
    return 0, messages


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


def valid_managed_agent_name(value: Any) -> bool:
    return isinstance(value, str) and value in {source.name for source in source_files()}


def install(codex_home: Path, apply: bool) -> tuple[int, list[str]]:
    agents, _, manifest_path = paths(codex_home)
    manifest = load_manifest(manifest_path)
    config_errors, planned_config_messages = install_config(codex_home, False, manifest)
    if config_errors:
        return config_errors, planned_config_messages
    agent_messages: list[str] = []
    errors = 0
    planned_agents: list[tuple[Path, Path, str, str]] = []
    for source in source_files():
        target = agents / source.name
        source_hash = digest(source)
        entry = manifest["files"].get(source.name, {})
        current_hash = digest(target) if target.exists() else None
        owned_old_version = bool(
            target.exists()
            and isinstance(entry, dict)
            and entry.get("status") == "active"
            and current_hash == entry.get("sha256")
        )
        if target.exists() and current_hash != source_hash and not owned_old_version:
            agent_messages.append(
                f"CONFLICT {target}: existing file differs from both current source and owned version; left untouched"
            )
            errors += 1
            continue
        if target.exists() and current_hash == source_hash:
            action = "KEEP"
        elif target.exists():
            action = "UPDATE" if apply else "WOULD_UPDATE"
        else:
            action = "INSTALL" if apply else "WOULD_INSTALL"
        agent_messages.append(f"{action} {target}")
        planned_agents.append((source, target, source.name, source_hash))
    if errors:
        return errors, agent_messages
    runtime_errors, runtime_messages = install_runtime(codex_home, apply, manifest)
    if runtime_errors:
        return runtime_errors, runtime_messages + agent_messages
    messages = runtime_messages + agent_messages
    if apply:
        for source, target, name, source_hash in planned_agents:
            if not target.is_file() or digest(target) != source_hash:
                atomic_copy(source, target)
            manifest["files"][name] = {"sha256": source_hash, "status": "active"}
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
        if not valid_managed_agent_name(name) or not isinstance(entry, dict):
            messages.append(f"PRESERVE {name}: invalid managed agent manifest entry")
            errors += 1
            continue
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
        if not valid_managed_agent_name(name) or not isinstance(entry, dict):
            messages.append(f"PRESERVE {name}: invalid managed agent manifest entry")
            errors += 1
            continue
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
    for directory, label in ((agents, "agents root"), (disabled, "disabled agents root")):
        if _lexists(directory) and (directory.is_symlink() or not directory.is_dir()):
            messages.append(f"PRESERVE {directory}: {label} is not a real directory")
            errors += 1
    if errors:
        return errors, messages
    for name, entry in sorted(manifest["files"].items()):
        if not valid_managed_agent_name(name) or not isinstance(entry, dict):
            messages.append(f"PRESERVE {name}: invalid managed agent manifest entry")
            errors += 1
            continue
        location = (disabled if entry.get("status") == "disabled" else agents) / name
        if not _lexists(location):
            continue
        if location.is_symlink() or not location.is_file() or digest(location) != entry.get("sha256"):
            messages.append(f"PRESERVE {location}: modified since installation")
            errors += 1
    config_errors, config_messages = check_uninstall_config(codex_home, manifest)
    errors += config_errors
    messages.extend(config_messages)
    runtime_errors, runtime_messages = check_uninstall_runtime(codex_home, manifest)
    errors += runtime_errors
    messages.extend(runtime_messages)
    if errors:
        return errors, messages

    remaining: dict[str, Any] = {}
    for name, entry in sorted(manifest["files"].items()):
        if not valid_managed_agent_name(name):
            # The preflight above guarantees this branch is unreachable.
            continue
        location = (disabled if entry.get("status") == "disabled" else agents) / name
        if not location.exists():
            messages.append(f"FORGET {name}: already absent")
            continue
        location.unlink()
        messages.append(f"REMOVED {location}")
    manifest["files"] = remaining
    config_errors, config_messages = uninstall_config(codex_home, manifest)
    errors += config_errors
    messages.extend(config_messages)
    runtime_errors, runtime_messages = uninstall_runtime(codex_home, manifest)
    errors += runtime_errors
    messages.extend(runtime_messages)
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
