#!/usr/bin/env python3
"""Local diagnostic/controller for Smart Router session state."""

from __future__ import annotations

import argparse
import json

from router_core import classify, data_root, load_state, set_mode


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or change Codex Smart Router state")
    parser.add_argument("command", choices=("status", "on", "shadow", "off", "route"))
    parser.add_argument("--session-id", required=True, help="Codex session id")
    parser.add_argument("--data-dir", help="Override plugin data directory")
    parser.add_argument("--prompt", help="Prompt to classify for the route command")
    args = parser.parse_args()
    root = data_root(args.data_dir)
    if args.command in {"on", "shadow", "off"}:
        state = set_mode(root, args.session_id, args.command.upper())
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if args.command == "route":
        if not args.prompt:
            parser.error("route requires --prompt")
        print(json.dumps(classify(args.prompt), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(load_state(root, args.session_id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
