#!/usr/bin/env python3
"""Securely configure or inspect the local GLM credential used by Smart Router."""

from __future__ import annotations

import argparse
import getpass
import os
import tempfile

from provider_policy import GLM_ENV_KEY, key_fingerprint, secret_path


def write_secret(value: str) -> None:
    path = secret_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"{GLM_ENV_KEY}={value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Show presence and fingerprint only")
    args = parser.parse_args()
    path = secret_path()
    if args.status:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            print(f"GLM credential: missing ({path})")
            return 1
        value = next((line.split("=", 1)[1].strip() for line in raw.splitlines() if line.startswith(f"{GLM_ENV_KEY}=")), "")
        if not value:
            print(f"GLM credential: missing ({path})")
            return 1
        print(f"GLM credential: configured; fingerprint={key_fingerprint(value)}; file={path}")
        return 0
    value = getpass.getpass("GLM Coding Plan API Key: ").strip()
    if not value:
        parser.error("empty key was not saved")
    write_secret(value)
    print(f"GLM credential saved with mode 0600: {path}; fingerprint={key_fingerprint(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
