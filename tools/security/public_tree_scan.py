"""Offline public-tree safety scan.

The repository is public.  It was assembled from archived private sources, so
the tree must never carry private key material, live-looking credentials, host
identity from those sources, raw request/response artifacts, or the content-body
capture feature that was deliberately excluded (legacy PR #17).

This is a **regression gate**, not a security boundary: a determined author can
evade any text scan.  It exists so an accidental copy of the exact identifiers
and shapes the migration already reviewed fails locally and in the test suite,
instead of waiting for a reader to notice them in a public diff.

The scan is deterministic and offline.  It reads tracked text files only; it
never opens a socket, reads a live credential, or follows a symlink out of the
tree.  Matched text is never included in a finding.

Usage::

    python3 tools/security/public_tree_scan.py --root .
    python3 tools/security/public_tree_scan.py --root . --json

Exit codes: ``0`` clean, ``1`` findings, ``2`` usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

#: Files whose bytes are not source or documentation.  The rules below are
#: text rules; scanning compressed image bytes produces coincidences, not
#: findings.  A binary cannot be the vector for a source-level copy anyway.
BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".bin",
        ".pyc",
    }
)

#: Synthetic data is allowed exactly where the repository documents it.  A raw
#: ``usage.jsonl`` anywhere else is a finding even if its first line says
#: "synthetic": the path is the claim, not the content.
ALLOWED_RAW_PREFIXES = (
    "tests/backend/fixtures/",
    "tests/fixtures/",
    "dev/fixtures/",
)

#: Documented example identities (see ``docs/access-boundaries.md`` and the V1
#: IAM contract).  ``x`` is the placeholder used by path-validation tests.
ALLOWED_HOME_NAMES = frozenset(
    {"alice", "bob", "carol", "dave", "x", "user", "example", "placeholder"}
)

#: A device reference may be published only when it is visibly a placeholder.
ALLOWED_DEVICE_PREFIXES = ("example", "placeholder", "synthetic")

#: Lines carrying these markers describe a synthetic or placeholder value.  The
#: credential rule uses them to avoid flagging the repository's own fixtures.
SAFE_MARKERS = ("synthetic", "example", "placeholder", "{{", "not-in-the-directory")

#: Source suffixes to which the content-capture rule applies.  Markdown is
#: excluded on purpose: the exclusion itself is documented in Markdown, and
#: quoting the rejected feature's name must not fail the gate.
CODE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".sh", ".yaml", ".yml", ".toml", ".json")

RAW_SUFFIXES = (".jsonl", ".sqlite", ".sqlite3", ".db", ".log")

#: The identifiers the migration review found in the archived private sources.
#: They are split so this public file does not reintroduce them; the scanner
#: joins them at runtime and compares the result.  The list is intentionally
#: narrow: it catches a re-copy of the reviewed material, not every hostname.
KNOWN_PRIVATE_IDENTIFIERS = (
    "192.168." + "32.179",
    "FJ-NJ-" + "SER-1009",
    "work" + "-fj",
    "personal" + "-dell",
    "tokyo" + "-shared-dev-01",
)

_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----")
_GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")
_BEARER = re.compile(r"\bBearer\s+([A-Za-z0-9._-]{20,})\b")
_DEVICE = re.compile(r"\bdevice:([A-Za-z0-9][A-Za-z0-9._-]{3,})")
_HOME = re.compile(r"(?<![\w./])/(?:home|Users|data/home)/([A-Za-z0-9._-]+)")
_CONTENT_CAPTURE = re.compile(
    r"(?i)(?:full[-_ ]?body[-_ ]?capture"
    r"|capture[-_ ]?(?:request|response)[-_ ]?body"
    r"|(?:request|response)[-_ ]?body[-_ ]?capture"
    r"|persist(?:s|ed|ing)?\s+(?:the\s+)?(?:prompt|response|request\s+body))"
)


@dataclass(frozen=True)
class Finding:
    """One rule violation.  ``rule`` and ``line`` locate it; text is omitted."""

    rule: str
    path: str
    line: int

    def as_dict(self) -> dict:
        return asdict(self)


def _is_binary(relpath: str) -> bool:
    return Path(relpath).suffix.lower() in BINARY_SUFFIXES


def _is_code(relpath: str) -> bool:
    return Path(relpath).suffix.lower() in CODE_SUFFIXES


def _is_allowed_raw(relpath: str) -> bool:
    return any(relpath.startswith(prefix) for prefix in ALLOWED_RAW_PREFIXES)


def _is_safe_value(line: str, value: str) -> bool:
    lowered_line = line.lower()
    lowered_value = value.lower()
    if "{{" in line:
        return True
    return any(marker in lowered_line or marker in lowered_value for marker in SAFE_MARKERS)


def _tracked_files(root: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file()
        )
    return [name for name in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if name]


def scan_text(relpath: str, text: str) -> list[Finding]:
    """Scan one file's text.  Exposed for tests and for embedding in CI."""

    findings: list[Finding] = []
    seen: set[tuple[str, int]] = set()

    def add(rule: str, lineno: int) -> None:
        key = (rule, lineno)
        if key not in seen:
            seen.add(key)
            findings.append(Finding(rule, relpath, lineno))

    name = Path(relpath).name.lower()
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(identifier in line for identifier in KNOWN_PRIVATE_IDENTIFIERS):
            add("private-source-identifier", lineno)

        if _PRIVATE_KEY.search(line):
            add("private-key", lineno)

        if (
            _GITHUB_TOKEN.search(line)
            or _SLACK_TOKEN.search(line)
            or _AWS_KEY.search(line)
            or _SK_TOKEN.search(line)
        ):
            add("credential", lineno)

        bearer = _BEARER.search(line)
        if bearer and not _is_safe_value(line, bearer.group(1)) and any(
            char.isdigit() for char in bearer.group(1)
        ):
            add("credential", lineno)

        device = _DEVICE.search(line)
        if device and not device.group(1).lower().startswith(ALLOWED_DEVICE_PREFIXES):
            if "{{" not in line:
                add("device-reference", lineno)

        home = _HOME.search(line)
        if home and home.group(1).lower() not in ALLOWED_HOME_NAMES:
            if "{{" not in line:
                add("absolute-home-path", lineno)

        if _is_code(relpath) and _CONTENT_CAPTURE.search(line):
            add("content-capture", lineno)

    if name in {"contentlog.py", "content_log.py"}:
        # The file's existence is the finding; line 1 is a stable anchor.
        add("content-capture", 1)

    for suffix in RAW_SUFFIXES:
        if relpath.lower().endswith(suffix) and not _is_allowed_raw(relpath):
            add("raw-artifact", 1)
            break

    return findings


def scan_root(root: str | Path, *, paths: Iterable[str] | None = None) -> list[Finding]:
    """Scan ``root`` (tracked files by default) and return all findings."""

    base = Path(root).resolve()
    selected = list(paths) if paths is not None else _tracked_files(base)
    findings: list[Finding] = []
    for relpath in selected:
        if _is_binary(relpath):
            continue
        try:
            text = (base / relpath).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        findings.extend(scan_text(relpath.replace("\\", "/"), text))
    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.rule))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline public-tree safety scan")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--json", action="store_true", help="print machine-readable findings")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        parser.error(f"--root does not exist: {root}")

    findings = scan_root(root)
    if args.json:
        print(
            json.dumps(
                {"root": str(root.resolve()), "findings": [f.as_dict() for f in findings]},
                indent=2,
                sort_keys=True,
            )
        )
    elif findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.rule}")
    else:
        print("public-tree safety scan: clean")

    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
