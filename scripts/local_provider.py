#!/usr/bin/env python3
"""Validated local text-provider configuration and an isolated circuit breaker."""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit


CONFIG_SCHEMA_VERSION = 1
HEALTH_SCHEMA_VERSION = 1
LOCAL_COOLDOWN_SECONDS = 60
LOCAL_AUTH_COOLDOWN_SECONDS = 300


@dataclass(frozen=True)
class LocalProviderConfig:
    provider_id: str
    display_name: str
    base_url: str
    model: str
    wire_api: str = "responses"
    env_key: str | None = None
    reasoning_effort: str = "medium"
    context_window: int = 131072
    allow_insecure_http: bool = False
    surrogate: str | None = None


def codex_home(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def config_path(home: str | Path | None = None) -> Path:
    return codex_home(home) / "smart-router" / "local-provider.json"


def model_catalog_path(home: str | Path | None = None) -> Path:
    return codex_home(home) / "smart-router" / "local-models.json"


def health_path(home: str | Path | None = None) -> Path:
    return codex_home(home) / "smart-router" / "local-provider-health.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def _safe_url(value: Any, allow_insecure_http: bool) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(char) < 32 for char in value):
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if parsed.scheme == "http":
        local_host = parsed.hostname.lower() == "localhost"
        try:
            address = ipaddress.ip_address(parsed.hostname)
            local_host = address.is_private or address.is_loopback or address.is_link_local
        except ValueError:
            pass
        if not local_host and not allow_insecure_http:
            return None
    return value.rstrip("/")


def validate_config(value: Any) -> tuple[LocalProviderConfig | None, str]:
    if not isinstance(value, dict):
        return None, "not_an_object"
    allowed = {
        "schema_version",
        "provider_id",
        "display_name",
        "base_url",
        "model",
        "wire_api",
        "env_key",
        "reasoning_effort",
        "context_window",
        "allow_insecure_http",
        "surrogate",
    }
    if set(value) - allowed or value.get("schema_version") != CONFIG_SCHEMA_VERSION:
        return None, "schema_invalid"
    provider_id = value.get("provider_id")
    if not isinstance(provider_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", provider_id):
        return None, "provider_id_invalid"
    if provider_id in {"openai", "zhipu_glm_coding"}:
        return None, "provider_id_reserved"
    display_name = value.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 80:
        return None, "display_name_invalid"
    if any(ord(char) < 32 or char in {'"', "'", "\\"} for char in display_name):
        return None, "display_name_invalid"
    model = value.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}", model):
        return None, "model_invalid"
    wire_api = value.get("wire_api", "responses")
    if wire_api != "responses":
        return None, "wire_api_unsupported"
    env_key = value.get("env_key")
    if env_key is not None and (not isinstance(env_key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", env_key)):
        return None, "env_key_invalid"
    if env_key is not None:
        credential_name = re.fullmatch(
            r"[A-Z][A-Z0-9_]*(?:^|_)(?:API_KEY|KEY|TOKEN|SECRET|CREDENTIALS?)",
            env_key,
        )
        reserved = (
            env_key in {"PATH", "HOME", "CODEX_HOME", "SHELL", "BASH_ENV", "ENV", "IFS", "CDPATH"}
            or env_key.startswith(("LD_", "DYLD_", "PYTHON", "BASH_FUNC_"))
            or env_key in {"NODE_OPTIONS", "RUBYOPT", "PERL5OPT", "JAVA_TOOL_OPTIONS"}
        )
        if credential_name is None or reserved:
            return None, "env_key_unsafe"
    effort = value.get("reasoning_effort", "medium")
    if effort not in {"low", "medium", "high", "max"}:
        return None, "reasoning_effort_invalid"
    context_window = value.get("context_window", 131072)
    if type(context_window) is not int or not 8192 <= context_window <= 2_000_000:
        return None, "context_window_invalid"
    allow_insecure_http = value.get("allow_insecure_http", False)
    if type(allow_insecure_http) is not bool:
        return None, "allow_insecure_http_invalid"
    base_url = _safe_url(value.get("base_url"), allow_insecure_http)
    if base_url is None:
        return None, "base_url_invalid"
    surrogate = value.get("surrogate")
    if surrogate is not None and (
        not isinstance(surrogate, str)
        or not surrogate.strip()
        or len(surrogate) > 80
        or any(ord(char) < 32 for char in surrogate)
    ):
        return None, "surrogate_invalid"
    return (
        LocalProviderConfig(
            provider_id=provider_id,
            display_name=display_name.strip(),
            base_url=base_url,
            model=model,
            wire_api=wire_api,
            env_key=env_key,
            reasoning_effort=effort,
            context_window=context_window,
            allow_insecure_http=allow_insecure_http,
            surrogate=surrogate.strip() if isinstance(surrogate, str) else None,
        ),
        "configured",
    )


def load_config(home: str | Path | None = None) -> tuple[LocalProviderConfig | None, str]:
    path = config_path(home)
    catalog_path = model_catalog_path(home)
    for candidate in (path, catalog_path):
        try:
            details = candidate.lstat()
        except FileNotFoundError:
            return None, "missing" if candidate == path else "model_catalog_missing"
        except OSError:
            return None, "unreadable"
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            return None, "unsafe_file_type"
        if details.st_mode & 0o077:
            return None, "unsafe_permissions"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "unreadable"
    config, reason = validate_config(value)
    if config is not None:
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, "model_catalog_invalid"
        if catalog != model_catalog(config):
            return None, "model_catalog_mismatch"
    return config, reason


def config_payload(config: LocalProviderConfig) -> dict[str, Any]:
    return {"schema_version": CONFIG_SCHEMA_VERSION, **asdict(config)}


def model_catalog(config: LocalProviderConfig) -> dict[str, Any]:
    return {
        "models": [
            {
                "slug": config.model,
                "display_name": config.model,
                "description": f"Text-only model routed through {config.display_name}",
                "default_reasoning_level": config.reasoning_effort,
                "supported_reasoning_levels": [
                    {"effort": effort, "description": f"{effort.title()} reasoning"}
                    for effort in ("low", "medium", "high", "max")
                ],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 0,
                "base_instructions": "",
                "supports_reasoning_summaries": True,
                "default_reasoning_summary": "none",
                "support_verbosity": False,
                "apply_patch_tool_type": "freeform",
                "truncation_policy": {"mode": "bytes", "limit": 10000},
                "context_window": config.context_window,
                "max_context_window": config.context_window,
                "effective_context_window_percent": 95,
                "supports_parallel_tool_calls": True,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
            }
        ]
    }


def write_config(config: LocalProviderConfig, home: str | Path | None = None) -> None:
    validated, reason = validate_config(config_payload(config))
    if validated is None:
        raise ValueError(f"invalid local provider config: {reason}")
    _atomic_json(model_catalog_path(home), model_catalog(validated))
    _atomic_json(config_path(home), config_payload(validated))


def parse_private_env(path: Path) -> dict[str, str]:
    try:
        if path.stat().st_mode & 0o077:
            return {}
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    result: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            result[key] = value
    return result


def provider_key(
    config: LocalProviderConfig,
    env: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> str | None:
    if not config.env_key:
        return None
    source = env if env is not None else os.environ
    direct = source.get(config.env_key)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    path = codex_home(home) / "smart-router" / "providers.env"
    value = parse_private_env(path).get(config.env_key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def config_fingerprint(config: LocalProviderConfig) -> str:
    raw = json.dumps(config_payload(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def key_fingerprint(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16] if value else None


@contextmanager
def _health_lock(home: str | Path | None = None) -> Iterator[None]:
    path = codex_home(home) / "smart-router" / "local-provider-health.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _default_health(config: LocalProviderConfig | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"schema_version": HEALTH_SCHEMA_VERSION, "state": "closed", "generation": 0}
    if config is not None:
        value["config_fingerprint"] = config_fingerprint(config)
    return value


def _load_health_unlocked(home: str | Path | None = None) -> dict[str, Any]:
    try:
        value = json.loads(health_path(home).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return _default_health()
    if not isinstance(value, dict) or value.get("schema_version") != HEALTH_SCHEMA_VERSION:
        return _default_health()
    value.setdefault("generation", 0)
    return value


def health_available(
    config: LocalProviderConfig,
    key: str | None,
    now_epoch: int | None = None,
    home: str | Path | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    now_value = int(time.time()) if now_epoch is None else int(now_epoch)
    fingerprint = config_fingerprint(config)
    with _health_lock(home):
        state = _load_health_unlocked(home)
        if state.get("config_fingerprint") not in {None, fingerprint} or (
            state.get("key_fingerprint") and state.get("key_fingerprint") != key_fingerprint(key)
        ):
            state = {
                **_default_health(config),
                "generation": int(state.get("generation") or 0) + 1,
                "updated_at": now_value,
            }
            _atomic_json(health_path(home), state)
            return True, "configuration_changed", state
        if state.get("state") == "closed":
            return True, "healthy", state
        if state.get("state") == "half_open" and int(state.get("probe_until") or 0) > now_value:
            return False, "probe_in_progress", state
        retry_after = int(state.get("retry_after") or 0)
        if retry_after and retry_after <= now_value:
            state["state"] = "half_open"
            state["generation"] = int(state.get("generation") or 0) + 1
            state["probe_until"] = now_value + 120
            state["updated_at"] = now_value
            _atomic_json(health_path(home), state)
            return True, "circuit_probe", state
        return False, str(state.get("reason") or "circuit_open"), state


def record_success(
    config: LocalProviderConfig,
    home: str | Path | None = None,
    now_epoch: int | None = None,
    expected_generation: int | None = None,
) -> bool:
    now_value = int(time.time()) if now_epoch is None else int(now_epoch)
    with _health_lock(home):
        state = _load_health_unlocked(home)
        generation = int(state.get("generation") or 0)
        if expected_generation is not None and generation != expected_generation:
            return False
        if expected_generation is None and state.get("state") != "closed":
            return False
        _atomic_json(
            health_path(home),
            {
                **_default_health(config),
                "generation": generation + 1,
                "updated_at": now_value,
            },
        )
        return True


def record_failure(
    config: LocalProviderConfig,
    detail: str,
    key: str | None,
    home: str | Path | None = None,
    now_epoch: int | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    now_value = int(time.time()) if now_epoch is None else int(now_epoch)
    lowered = detail.lower()
    authentication = any(marker in lowered for marker in ("401", "unauthorized", "invalid api key", "authentication"))
    reason = "authentication" if authentication else "runtime_failure"
    cooldown = LOCAL_AUTH_COOLDOWN_SECONDS if authentication else LOCAL_COOLDOWN_SECONDS
    with _health_lock(home):
        current = _load_health_unlocked(home)
        if expected_generation is not None and int(current.get("generation") or 0) != expected_generation:
            return {**current, "ignored_stale_failure": True}
        current_fingerprint = current.get("config_fingerprint")
        if current_fingerprint not in {None, config_fingerprint(config)}:
            return {**current, "ignored_stale_failure": True}
        state = {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "state": "open",
            "reason": reason,
            "retry_after": now_value + cooldown,
            "key_fingerprint": key_fingerprint(key),
            "config_fingerprint": config_fingerprint(config),
            "generation": int(current.get("generation") or 0) + 1,
            "updated_at": now_value,
        }
        _atomic_json(health_path(home), state)
        return state


def read_health(home: str | Path | None = None) -> dict[str, Any]:
    with _health_lock(home):
        return dict(_load_health_unlocked(home))
