#!/usr/bin/env python3
"""Validate a receipt from a file or stdin."""

from __future__ import annotations

import argparse
import json
import sys

from router_core import validate_receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="-", help="JSON receipt path; stdin when omitted")
    args = parser.parse_args()
    raw = open(args.path, encoding="utf-8").read() if args.path else sys.stdin.read()
    valid, errors, receipt = validate_receipt(raw)
    print(json.dumps({"valid": valid, "errors": errors, "receipt": receipt}, ensure_ascii=False, indent=2))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
