"""Command-line entry for the release contract (offline only).

    python3 -m tools.release.cli validate deploy/llmlab/release.example.json
    python3 -m tools.release.cli ready    deploy/llmlab/release.example.json

Exit codes: 0 valid / READY, 1 invalid manifest, 3 valid but NOT_READY.
The tool opens no socket and starts nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract import ContractError, validate_manifest


def _load(path: str):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractError(f"cannot read manifest: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"manifest is not valid JSON: {exc.msg}") from exc
    return validate_manifest(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m tools.release.cli")
    parser.add_argument("command", choices=("validate", "ready"))
    parser.add_argument("manifest")
    args = parser.parse_args(argv)
    try:
        manifest = _load(args.manifest)
    except ContractError as exc:
        print(json.dumps({"state": "INVALID", "reason": str(exc)}))
        return 1
    if args.command == "validate":
        print(json.dumps({
            "state": "VALID",
            "package": f"{manifest.package_name}@{manifest.package_version}",
            "git_sha": manifest.git_sha,
            "origin": f"http://{manifest.bind_host}:{manifest.port}/",
            "mounts": {"static": "/", "api": manifest.mount_plan.api_prefix,
                       "platform_auth": manifest.mount_plan.platform_prefix},
            "linux_auth_mounted": False,
        }))
        return 0
    report = manifest.readiness()
    print(json.dumps(report))
    return 0 if report["state"] == "READY" else 3


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
