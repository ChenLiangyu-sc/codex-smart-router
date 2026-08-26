#!/usr/bin/env python3
"""Provider selection, GLM schedule policy, secrets, and circuit breaking."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from local_provider import (
    LocalProviderConfig,
    config_fingerprint as local_config_fingerprint,
    health_available as local_health_available,
    load_config as load_local_config,
    model_catalog_path as local_model_catalog_path,
    provider_key as local_provider_key,
)


PROFILE_STABLE = "STABLE"
PROFILE_GLM_FIRST = "GLM_FIRST"
EXECUTION_PROFILES = {PROFILE_STABLE, PROFILE_GLM_FIRST}

LIGHT_PROFILE_LUNA_STABLE = "LUNA_STABLE"
LIGHT_PROFILE_LOCAL_TEXT_FIRST = "LOCAL_TEXT_FIRST"
LIGHT_PROFILES = {LIGHT_PROFILE_LUNA_STABLE, LIGHT_PROFILE_LOCAL_TEXT_FIRST}

GLM_ENV_KEY = "ZHIPU_API_KEY"
GLM_PROVIDER_ID = "zhipu_glm_coding"
GLM_BASE_URL = "https://open.bigmodel.cn/api/v1"
GLM_MODEL = "glm-5.3"

RECEIPT_STRICT_JSON_SCHEMA = "strict_json_schema"
RECEIPT_JSON_OBJECT_ADAPTER = "json_object_adapter"

LUNA_ROLES = {"router_scout", "router_monitor", "router_tester", "router_docs"}
LOCAL_TEXT_ELIGIBLE_ROLES = {"router_scout", "router_monitor"}
GLM_ELIGIBLE_ROLES = {"router_worker", "router_reviewer"}

QUOTA_5H_CODES = {1308, 1316, 1318, 1320}
QUOTA_7D_CODES = {1310, 1317, 1319, 1321}
QUOTA_CODES = QUOTA_5H_CODES | QUOTA_7D_CODES
TRANSIENT_CODES = {1302, 1305}
AUTH_CODES = {1000, 1001, 1003}
SUBSCRIPTION_CODES = {1309, 1314, 1315}


@dataclass(frozen=True)
class ExecutorSpec:
    id: str
    provider: str
    model: str
    reasoning_effort: str
    sandbox: str
    route_label: str
    provider_name: str | None = None
    base_url: str | None = None
    env_key: str | None = None
    wire_api: str = "responses"
    model_catalog: str | None = None
    provider_fingerprint: str | None = None
    receipt_mode: str = RECEIPT_STRICT_JSON_SCHEMA


@dataclass(frozen=True)
class Resolution:
    executor: ExecutorSpec
    requested_profile: str
    reason: str
    glm_available: bool
    health_generation: int = 0
    requested_light_profile: str = LIGHT_PROFILE_LUNA_STABLE
    local_available: bool = False
    local_config: LocalProviderConfig | None = None


EXECUTORS = {
    "luna_scout": ExecutorSpec("luna_scout", "openai", "gpt-5.6-luna", "medium", "read-only", "Luna · 只读侦察"),
    "luna_monitor": ExecutorSpec("luna_monitor", "openai", "gpt-5.6-luna", "low", "read-only", "Luna · 监控"),
    "luna_tester": ExecutorSpec("luna_tester", "openai", "gpt-5.6-luna", "medium", "workspace-write", "Luna · 测试"),
    "luna_docs": ExecutorSpec("luna_docs", "openai", "gpt-5.6-luna", "medium", "workspace-write", "Luna · 文档"),
    "terra_worker": ExecutorSpec("terra_worker", "openai", "gpt-5.6-terra", "medium", "workspace-write", "Terra · 执行"),
    "terra_reviewer": ExecutorSpec("terra_reviewer", "openai", "gpt-5.6-terra", "high", "read-only", "Terra · 审查"),
    "glm_worker": ExecutorSpec(
        "glm_worker", GLM_PROVIDER_ID, GLM_MODEL, "max", "workspace-write", "GLM-5.3 Max · 执行",
        "Zhipu GLM Coding Plan", GLM_BASE_URL, GLM_ENV_KEY,
        receipt_mode=RECEIPT_JSON_OBJECT_ADAPTER,
    ),
    "glm_reviewer": ExecutorSpec(
        "glm_reviewer", GLM_PROVIDER_ID, GLM_MODEL, "max", "read-only", "GLM-5.3 Max · 审查",
        "Zhipu GLM Coding Plan", GLM_BASE_URL, GLM_ENV_KEY,
        receipt_mode=RECEIPT_JSON_OBJECT_ADAPTER,
    ),
}

ROLE_EXECUTORS = {
    "router_scout": "luna_scout",
    "router_monitor": "luna_monitor",
    "router_tester": "luna_tester",
    "router_docs": "luna_docs",
}

DEFAULT_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "timezone": "Asia/Shanghai",
    "peak_weekdays": [0, 1, 2, 3, 4],
    "peak_start": "14:00",
    "peak_end": "18:00",
    "transient_cooldown_seconds": 120,
    "subscription_cooldown_seconds": 21600,
    "glm_base_url": GLM_BASE_URL,
    "allow_insecure_glm_http": False,
}


def codex_home(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def policy_path(home: str | Path | None = None) -> Path:
    return codex_home(home) / "smart-router" / "policy.json"


def secret_path(home: str | Path | None = None) -> Path:
    configured = os.environ.get("CODEX_SMART_ROUTER_ENV_FILE")
    return Path(configured).expanduser().resolve() if configured else codex_home(home) / "smart-router" / "providers.env"


def health_path(home: str | Path | None = None) -> Path:
    return codex_home(home) / "smart-router" / "provider-health.json"


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


@contextmanager
def _health_lock(home: str | Path | None = None) -> Iterator[None]:
    path = codex_home(home) / "smart-router" / "provider-health.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_policy(home: str | Path | None = None) -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    try:
        custom = json.loads(policy_path(home).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return policy
    except (OSError, json.JSONDecodeError):
        return {**policy, "invalid": True}
    if not isinstance(custom, dict):
        return {**policy, "invalid": True}
    allowed = set(DEFAULT_POLICY)
    if set(custom) - allowed:
        return {**policy, "invalid": True}
    if "schema_version" in custom and custom["schema_version"] != 1:
        return {**policy, "invalid": True}
    if "timezone" in custom:
        timezone = custom["timezone"]
        if not isinstance(timezone, str):
            return {**policy, "invalid": True}
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            return {**policy, "invalid": True}
        policy["timezone"] = timezone
    if "peak_weekdays" in custom:
        weekdays = custom["peak_weekdays"]
        if not (
            isinstance(weekdays, list)
            and weekdays
            and all(type(value) is int and 0 <= value <= 6 for value in weekdays)
        ):
            return {**policy, "invalid": True}
        policy["peak_weekdays"] = sorted(set(weekdays))
    for key in ("peak_start", "peak_end"):
        if key in custom:
            value = custom[key]
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                return {**policy, "invalid": True}
            policy[key] = value
    if _clock_time(policy["peak_start"]) >= _clock_time(policy["peak_end"]):
        return {**policy, "invalid": True}
    for key in ("transient_cooldown_seconds", "subscription_cooldown_seconds"):
        if key in custom:
            value = custom[key]
            if type(value) is not int or not 1 <= value <= 7 * 24 * 60 * 60:
                return {**policy, "invalid": True}
            policy[key] = value
    if "allow_insecure_glm_http" in custom:
        if type(custom["allow_insecure_glm_http"]) is not bool:
            return {**policy, "invalid": True}
        policy["allow_insecure_glm_http"] = custom["allow_insecure_glm_http"]
    if "glm_base_url" in custom:
        base_url = _safe_provider_url(
            custom["glm_base_url"],
            bool(policy["allow_insecure_glm_http"]),
        )
        if base_url is None:
            return {**policy, "invalid": True}
        policy["glm_base_url"] = base_url
    return policy


def _safe_provider_url(value: Any, allow_insecure_http: bool) -> str | None:
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


def _clock_time(value: str) -> dt.time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return dt.time(hour, minute)


def local_now(policy: Mapping[str, Any], now: dt.datetime | None = None) -> dt.datetime:
    try:
        zone = ZoneInfo(str(policy["timezone"]))
    except (KeyError, ZoneInfoNotFoundError):
        zone = ZoneInfo("Asia/Shanghai")
    if now is None:
        return dt.datetime.now(zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def is_peak_window(now: dt.datetime | None = None, policy: Mapping[str, Any] | None = None) -> bool:
    active = dict(policy or DEFAULT_POLICY)
    current = local_now(active, now)
    if current.weekday() not in active["peak_weekdays"]:
        return False
    value = current.timetz().replace(tzinfo=None)
    return _clock_time(active["peak_start"]) <= value < _clock_time(active["peak_end"])


def _parse_env_file(path: Path) -> dict[str, str]:
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            return {}
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            values[key] = value
    return values


def glm_key(env: Mapping[str, str] | None = None, home: str | Path | None = None) -> str | None:
    source = env if env is not None else os.environ
    direct = source.get(GLM_ENV_KEY)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    value = _parse_env_file(secret_path(home)).get(GLM_ENV_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


def key_fingerprint(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16] if value else None


def glm_health_identity(key: str | None, base_url: str | None = None) -> str | None:
    if not key:
        return None
    return f"{key}\0{(base_url or GLM_BASE_URL).rstrip('/')}"


def _load_health_unlocked(home: str | Path | None = None) -> dict[str, Any]:
    try:
        value = json.loads(health_path(home).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"schema_version": 1, "state": "closed", "generation": 0}
    if not isinstance(value, dict):
        return {"schema_version": 1, "state": "closed", "generation": 0}
    value.setdefault("generation", 0)
    return value


def glm_health_available(
    key: str | None,
    now_epoch: int | None = None,
    home: str | Path | None = None,
    base_url: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    now_value = int(time.time()) if now_epoch is None else int(now_epoch)
    fingerprint = key_fingerprint(glm_health_identity(key, base_url))
    with _health_lock(home):
        state = _load_health_unlocked(home)
        if state.get("state") == "closed":
            return True, "healthy", state
        if state.get("key_fingerprint") and state.get("key_fingerprint") != fingerprint:
            state = {
                "schema_version": 1,
                "state": "closed",
                "generation": int(state.get("generation") or 0) + 1,
                "updated_at": now_value,
            }
            _atomic_json(health_path(home), state)
            return True, "key_changed", state
        if state.get("state") == "half_open":
            probe_until = int(state.get("probe_until") or 0)
            if probe_until > now_value:
                return False, "probe_in_progress", state
        retry_after = state.get("retry_after")
        if isinstance(retry_after, int) and retry_after <= now_value:
            state["state"] = "half_open"
            state["generation"] = int(state.get("generation") or 0) + 1
            state["probe_until"] = now_value + 300
            state["updated_at"] = now_value
            _atomic_json(health_path(home), state)
            return True, "circuit_probe", state
        return False, str(state.get("reason") or "circuit_open"), state


def record_glm_success(
    home: str | Path | None = None,
    now_epoch: int | None = None,
    expected_generation: int | None = None,
) -> bool:
    now_value = int(time.time()) if now_epoch is None else int(now_epoch)
    with _health_lock(home):
        current = _load_health_unlocked(home)
        generation = int(current.get("generation") or 0)
        if expected_generation is None:
            if current.get("state") != "closed":
                return False
        elif generation != expected_generation:
            return False
        _atomic_json(
            health_path(home),
            {
                "schema_version": 1,
                "state": "closed",
                "generation": generation + 1,
                "updated_at": now_value,
            },
        )
        return True


def _parse_retry_after(detail: str) -> int | None:
    for pattern in (
        r'"next_flush_time"\s*:\s*(\d{10,13})',
        r"next_flush_time[=:：\s]+(\d{10,13})",
    ):
        match = re.search(pattern, detail, re.I)
        if match:
            value = int(match.group(1))
            return value // 1000 if value > 10**12 else value
    match = re.search(
        r'next_flush_time["\s:=：]+([0-9]{4}-[0-9]{2}-[0-9]{2}[T\s][0-9]{2}:[0-9]{2}(?::[0-9]{2})?(?:Z|[+-][0-9]{2}:?[0-9]{2})?)',
        detail,
        re.I,
    )
    if not match:
        return None
    raw = match.group(1).replace(" ", "T")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(parsed.timestamp())


def parse_glm_error(detail: str) -> tuple[int | None, int | None]:
    code: int | None = None
    for pattern in (
        r'"(?:code|error_code)"\s*:\s*"?(\d{4})"?',
        r"(?:business|error)[ _-]?code[=:：\s]+(\d{4})",
        r"(?:^|\D)(1(?:00[013]|30[2589]|31[046789]|32[01]))(?:\D|$)",
    ):
        match = re.search(pattern, detail, re.I)
        if match:
            code = int(match.group(1))
            break
    return code, _parse_retry_after(detail)


def record_glm_failure(
    detail: str,
    key: str | None,
    home: str | Path | None = None,
    now_epoch: int | None = None,
    policy: Mapping[str, Any] | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    now_value = int(time.time()) if now_epoch is None else int(now_epoch)
    active = dict(policy or load_policy(home))
    code, supplied_retry = parse_glm_error(detail)
    normalized_detail = detail.lower()
    if code is None:
        if any(
            marker in normalized_detail
            for marker in ("401 unauthorized", "invalid api key", "invalid_api_key", "authentication failed")
        ):
            code = 1000
        elif any(
            marker in normalized_detail
            for marker in (
                "stream disconnected before completion",
                "connection reset",
                "connection refused",
                "temporarily unavailable",
                "timed out",
                "timeout",
                "transport channel closed",
                "error sending request",
                "429 too many requests",
            )
        ):
            code = 1302
    if code in QUOTA_5H_CODES:
        reason, retry_after = "quota_5h", supplied_retry or now_value + 5 * 60 * 60
    elif code in QUOTA_7D_CODES:
        reason, retry_after = "quota_7d", supplied_retry or now_value + 7 * 24 * 60 * 60
    elif code in TRANSIENT_CODES:
        reason, retry_after = "transient", now_value + int(active["transient_cooldown_seconds"])
    elif code in AUTH_CODES:
        reason, retry_after = "authentication", None
    elif code in SUBSCRIPTION_CODES:
        reason, retry_after = "subscription", now_value + int(active["subscription_cooldown_seconds"])
    else:
        return {"schema_version": 1, "state": "closed", "error_code": code, "reason": "unknown"}
    state = {
        "schema_version": 1,
        "state": "open",
        "reason": reason,
        "error_code": code,
        "retry_after": retry_after,
            "key_fingerprint": key_fingerprint(glm_health_identity(key, base_url)),
        "updated_at": now_value,
    }
    with _health_lock(home):
        current = _load_health_unlocked(home)
        state["generation"] = int(current.get("generation") or 0) + 1
        _atomic_json(health_path(home), state)
    return state


def read_health(home: str | Path | None = None) -> dict[str, Any]:
    with _health_lock(home):
        return dict(_load_health_unlocked(home))


def resolve_executor(
    role: str,
    profile: str = PROFILE_STABLE,
    *,
    light_profile: str = LIGHT_PROFILE_LUNA_STABLE,
    has_images: bool = False,
    now: dt.datetime | None = None,
    env: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> Resolution:
    normalized = profile.upper()
    if normalized not in EXECUTION_PROFILES:
        raise ValueError(f"unsupported execution profile: {profile}")
    normalized_light = light_profile.upper()
    if normalized_light not in LIGHT_PROFILES:
        raise ValueError(f"unsupported light profile: {light_profile}")
    if role in ROLE_EXECUTORS:
        luna = EXECUTORS[ROLE_EXECUTORS[role]]
        if role not in LOCAL_TEXT_ELIGIBLE_ROLES or normalized_light != LIGHT_PROFILE_LOCAL_TEXT_FIRST:
            return Resolution(
                luna,
                normalized,
                "luna_role",
                False,
                requested_light_profile=normalized_light,
            )
        config, config_reason = load_local_config(home)
        if config is None:
            return Resolution(
                luna,
                normalized,
                f"local_config_{config_reason}",
                False,
                requested_light_profile=normalized_light,
            )
        key = local_provider_key(config, env, home)
        if config.env_key and not key:
            return Resolution(
                luna,
                normalized,
                "local_key_missing",
                False,
                requested_light_profile=normalized_light,
                local_config=config,
            )
        epoch = int(now.timestamp()) if now is not None else int(time.time())
        available, reason, health = local_health_available(config, key, epoch, home)
        if not available:
            return Resolution(
                luna,
                normalized,
                f"local_{reason}",
                False,
                requested_light_profile=normalized_light,
                local_config=config,
            )
        role_suffix = "scout" if role == "router_scout" else "monitor"
        role_label = "只读侦察" if role == "router_scout" else "监控"
        executor = ExecutorSpec(
            f"local_{role_suffix}",
            config.provider_id,
            config.model,
            config.reasoning_effort,
            "read-only",
            f"{config.display_name} · {role_label}",
            config.display_name,
            config.base_url,
            config.env_key,
            config.wire_api,
            str(local_model_catalog_path(home)),
            local_config_fingerprint(config),
        )
        return Resolution(
            executor,
            normalized,
            reason,
            False,
            int(health.get("generation") or 0),
            normalized_light,
            True,
            config,
        )
    if role not in GLM_ELIGIBLE_ROLES:
        raise ValueError(f"unknown role: {role}")
    terra = EXECUTORS["terra_worker" if role == "router_worker" else "terra_reviewer"]
    glm = EXECUTORS["glm_worker" if role == "router_worker" else "glm_reviewer"]
    if normalized != PROFILE_GLM_FIRST:
        return Resolution(terra, normalized, "stable_profile", False, requested_light_profile=normalized_light)
    if has_images:
        return Resolution(terra, normalized, "multimodal_requires_terra", False, requested_light_profile=normalized_light)
    policy = load_policy(home)
    if policy.get("invalid"):
        return Resolution(terra, normalized, "invalid_policy", False, requested_light_profile=normalized_light)
    if is_peak_window(now, policy):
        return Resolution(terra, normalized, "glm_peak_window", False, requested_light_profile=normalized_light)
    glm = replace(glm, base_url=str(policy["glm_base_url"]))
    key = glm_key(env, home)
    if not key:
        return Resolution(terra, normalized, "glm_key_missing", False, requested_light_profile=normalized_light)
    epoch = int(local_now(policy, now).timestamp()) if now is not None else int(time.time())
    available, reason, health = glm_health_available(
        key,
        epoch,
        home,
        base_url=str(policy["glm_base_url"]),
    )
    if not available:
        return Resolution(terra, normalized, f"glm_{reason}", False, requested_light_profile=normalized_light)
    return Resolution(
        glm,
        normalized,
        reason,
        True,
        int(health.get("generation") or 0),
        normalized_light,
    )


def resolution_dict(value: Resolution) -> dict[str, Any]:
    return {
        "executor": asdict(value.executor),
        "requested_profile": value.requested_profile,
        "requested_light_profile": value.requested_light_profile,
        "reason": value.reason,
        "glm_available": value.glm_available,
        "local_available": value.local_available,
        "health_generation": value.health_generation,
    }


def redact_secrets(text: str, values: list[str | None] | None = None) -> str:
    result = text
    for value in values or []:
        if value:
            result = result.replace(value, "<redacted>")
    result = re.sub(r"[A-Za-z0-9]{24,}\.[A-Za-z0-9_-]{8,}", "<redacted>", result)
    return result
