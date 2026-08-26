#!/usr/bin/env python3
"""Configure the text-only light-task provider without storing credentials in the plugin."""

from __future__ import annotations

import argparse

from local_provider import LocalProviderConfig, config_path, load_config, model_catalog_path, write_config
from provider_policy import GLM_BASE_URL, GLM_ENV_KEY, GLM_MODEL


def glm_surrogate_config() -> LocalProviderConfig:
    return LocalProviderConfig(
        provider_id="local_text_glm_surrogate",
        display_name="GLM-5.3 surrogate",
        base_url=GLM_BASE_URL,
        model=GLM_MODEL,
        env_key=GLM_ENV_KEY,
        reasoning_effort="medium",
        context_window=1_048_576,
        surrogate="DeepSeek V4 Flash routing surrogate",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Show non-secret provider configuration status")
    mode.add_argument(
        "--glm-surrogate",
        action="store_true",
        help="Use the existing GLM-5.3 credential as a temporary local-text routing surrogate",
    )
    parser.add_argument("--provider-id")
    parser.add_argument("--display-name")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--env-key", help="Environment variable name; omit for a no-auth LAN endpoint")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "max"), default="medium")
    parser.add_argument("--context-window", type=int, default=131072)
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Explicitly allow an HTTP hostname; private/loopback IPs and localhost do not need this flag",
    )
    args = parser.parse_args()

    if args.status:
        config, reason = load_config()
        if config is None:
            print(f"Local text provider: unavailable ({reason}); config={config_path()}")
            return 1
        auth = f"env:{config.env_key}" if config.env_key else "no-auth"
        surrogate = f"; surrogate={config.surrogate}" if config.surrogate else ""
        print(
            f"Local text provider: configured; name={config.display_name}; model={config.model}; "
            f"wire_api={config.wire_api}; auth={auth}{surrogate}; config={config_path()}; "
            f"catalog={model_catalog_path()}"
        )
        return 0

    if args.glm_surrogate:
        config = glm_surrogate_config()
    else:
        missing = [
            flag
            for flag, value in (
                ("--provider-id", args.provider_id),
                ("--display-name", args.display_name),
                ("--base-url", args.base_url),
                ("--model", args.model),
            )
            if not value
        ]
        if missing:
            parser.error("custom configuration requires " + ", ".join(missing))
        config = LocalProviderConfig(
            provider_id=args.provider_id,
            display_name=args.display_name,
            base_url=args.base_url,
            model=args.model,
            env_key=args.env_key,
            reasoning_effort=args.reasoning_effort,
            context_window=args.context_window,
            allow_insecure_http=args.allow_insecure_http,
        )
    try:
        write_config(config)
    except ValueError as exc:
        parser.error(str(exc))
    print(
        f"Local text provider configured with mode 0600: {config_path()}; "
        f"model catalog: {model_catalog_path()}; credentials were not copied."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
