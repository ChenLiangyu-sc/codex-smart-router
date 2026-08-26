#!/usr/bin/env python3
"""Local diagnostic/controller for Smart Router session state."""

from __future__ import annotations

import argparse
import json

from provider_policy import (
    LIGHT_PROFILE_LOCAL_TEXT_FIRST,
    LIGHT_PROFILE_LUNA_STABLE,
    LUNA_BOUNDED,
    LUNA_DISABLED,
    PROFILE_GLM_FIRST,
    PROFILE_STABLE,
)
from router_core import (
    classify,
    data_root,
    load_state,
    set_economics_policy,
    set_execution_profile,
    set_light_profile,
    set_luna_mode,
    set_mode,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or change Codex Smart Router state")
    parser.add_argument(
        "command",
        choices=(
            "status",
            "on",
            "shadow",
            "off",
            "glm-on",
            "glm-off",
            "local-on",
            "local-off",
            "luna-on",
            "luna-off",
            "economics-v1",
            "economics-v2",
            "route",
        ),
    )
    parser.add_argument("--session-id", required=True, help="Codex session id")
    parser.add_argument("--data-dir", help="Override plugin data directory")
    parser.add_argument("--prompt", help="Prompt to classify for the route command")
    args = parser.parse_args()
    root = data_root(args.data_dir)
    if args.command in {"on", "shadow", "off"}:
        state = set_mode(root, args.session_id, args.command.upper())
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"glm-on", "glm-off"}:
        profile = PROFILE_GLM_FIRST if args.command == "glm-on" else PROFILE_STABLE
        state = set_execution_profile(root, args.session_id, profile, activate=args.command == "glm-on")
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"local-on", "local-off"}:
        profile = LIGHT_PROFILE_LOCAL_TEXT_FIRST if args.command == "local-on" else LIGHT_PROFILE_LUNA_STABLE
        state = set_light_profile(root, args.session_id, profile, activate=args.command == "local-on")
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"luna-on", "luna-off"}:
        mode = LUNA_BOUNDED if args.command == "luna-on" else LUNA_DISABLED
        state = set_luna_mode(root, args.session_id, mode, activate=args.command == "luna-on")
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"economics-v1", "economics-v2"}:
        policy = "V1_COMPAT" if args.command == "economics-v1" else "V2_STATIC"
        state = set_economics_policy(root, args.session_id, policy)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if args.command == "route":
        if not args.prompt:
            parser.error("route requires --prompt")
        state = load_state(root, args.session_id)
        print(
            json.dumps(
                classify(
                    args.prompt,
                    economics=True,
                    economics_policy=state["economics_policy"],
                    execution_profile=state["execution_profile"],
                    light_profile=state["light_profile"],
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(json.dumps(load_state(root, args.session_id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
