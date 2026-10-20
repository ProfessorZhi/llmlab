"""Offline public-tree safety scan.

The repository is public.  It was assembled from archived private sources, so
the tree must never carry private key material, live-looking credentials, host
identity from those sources, raw request/response artifacts, or the content-body
capture feature that was deliberately excluded (legacy PR #17).

Scope
-----
This scans the **current tracked worktree** (``git ls-files``).  It does not
walk Git history and it is not gitleaks or trufflehog: a clean result says the
checked-out tree is clean, not that every historical commit or deleted blob is.
A history secret scan is a separate control and is **not performed here**.

Tracked selection
-----------------
Default ``scan_root(root)`` selects files with ``git ls-files -z`` only.  If Git
is unavailable, the root is not a repository, the command times out, or it
returns non-zero, the scan fails closed with :class:`ScanReadError` and the CLI
exits 2.  It never falls back to walking the directory, so ignored/untracked
files and ``.git`` are not read.  Explicit ``paths=`` exists for isolated test
fixtures or an already-materialized file list; production callers should not
synthesize a selection.

Path safety
-----------
Every selected path is rejected if it is absolute, contains ``.``/``..``/empty
components, or has a symlink or Windows reparse-point component.  The resolved
path must stay inside the repository root.  A path that cannot be validated or
read is a scan failure (exit 2), never a silent skip.

Placeholder policy
------------------
Only the **matched value** may be a placeholder (``synthetic-...``,
``example-...``, ``placeholder-...``, ``{{...}}``, or the documented test
double).  A placeholder comment elsewhere on the line does not exempt a real
token, device id or home path.

This is a **regression gate**, not a security boundary: a determined author can
evade any text scan.  It exists so an accidental copy of the exact identifiers
and shapes the migration already reviewed fails locally and in the test suite,
instead of waiting for a reader to notice them in a public diff.

The scan is deterministic and offline.  In default mode it reads Git-tracked
text files only; it never opens a socket, reads a live credential, or follows a
link out of the tree.  Matched text is never included in a finding.

Usage::

    python3 tools/security/public_tree_scan.py --root .
    python3 tools/security/public_tree_scan.py --root . --json

Exit codes: ``0`` clean, ``1`` findings, ``2`` scan failure / usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
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

#: A value may be published only when it is visibly a placeholder.  The check
#: is applied to the matched value, never to the whole line.
PLACEHOLDER_PREFIXES = ("synthetic", "example", "placeholder")
PLACEHOLDER_EXACT = frozenset({"not-in-the-directory"})
_PLACEHOLDER_BRACED = re.compile(r"\{\{[^{}\s]+\}\}")

#: Source suffixes to which the content-capture rule applies.  Markdown is
#: excluded on purpose: the exclusion itself is documented in Markdown, and
#: quoting the rejected feature's name must not fail the gate.
CODE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".sh", ".yaml", ".yml", ".toml", ".json")

RAW_SUFFIXES = (".jsonl", ".sqlite", ".sqlite3", ".db", ".log")

#: Windows marks a reparse point (junction, mount point, symlink) with this bit.
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

#: Drive-letter prefixes are absolute paths on Windows and must be refused even
#: when this scanner runs on POSIX.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

#: SHA-256 (lower-case) of the identifiers the migration review found in the
#: archived private sources.  Only the digests live here: writing the
#: identifiers themselves would reintroduce exactly what this gate is meant to
#: keep out.  The list is intentionally narrow — it catches a re-copy of the
#: reviewed material, not every hostname.
KNOWN_PRIVATE_IDENTIFIER_SHA256 = frozenset(
    {
        "c023d0e26b192813a12b126299d36064dbfbf5c1c633324e7a230fbf3a1b36df",
        "a6e9641bec5e347de8e10e1897fc284ce7d00f325e33c35ae45fda6e613d0738",
        "9868077fb91011b78af615329b63b25f28b622db8d4769cc3e4d91fea52a70aa",
        "95147a0df6c54f2f07fc8dfb4040447e909e76927ef8580e4acffa54a62afdb5",
        "5ca34bbb2901acd553e678ed31fb17f9bb8baeb1906f0f670dfc124443ae32c6",
    }
)

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}")
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


class ScanError(Exception):
    """Base class for a fail-closed scan failure."""


class UnsafePathError(ScanError):
    """A selected path is absolute, traverses, or contains a link component."""


class ScanReadError(ScanError):
    """A selected path could not be read safely."""


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


def _is_placeholder_value(value: str) -> bool:
    """``True`` only for the matched value itself, never for its line."""

    if not isinstance(value, str) or not value:
        return False
    if value.lower() in PLACEHOLDER_EXACT:
        return True
    if _PLACEHOLDER_BRACED.fullmatch(value):
        return True
    lowered = value.lower()
    return any(
        lowered == prefix
        or lowered.startswith(prefix + "-")
        or lowered.startswith(prefix + "_")
        for prefix in PLACEHOLDER_PREFIXES
    )


def _validate_relpath(relpath: str) -> tuple[str, ...]:
    """Return canonical path components or raise :class:`UnsafePathError`."""

    if not isinstance(relpath, str) or not relpath:
        raise UnsafePathError("empty or non-string path")
    if "\x00" in relpath:
        raise UnsafePathError("NUL in path")
    if "\\" in relpath:
        raise UnsafePathError("backslash in path")
    if relpath.startswith("/") or _DRIVE_PREFIX.match(relpath):
        raise UnsafePathError("absolute path")
    if relpath.endswith("/"):
        raise UnsafePathError("trailing separator")
    if "//" in relpath:
        raise UnsafePathError("empty path component")
    if relpath == "." or relpath.startswith("./") or "/./" in relpath or relpath.endswith("/."):
        raise UnsafePathError("non-canonical '.' component")
    parts = PurePosixPath(relpath).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise UnsafePathError("path traversal or non-canonical component")
    return parts


def _safe_path(base: Path, relpath: str) -> Path:
    """Validate every component without following links; fail closed."""

    parts = _validate_relpath(relpath)
    current = base
    final_status = None
    for index, part in enumerate(parts):
        current = current / part
        try:
            final_status = os.lstat(current)
        except OSError as exc:
            raise ScanReadError(
                f"{relpath}: cannot stat ({exc.strerror or exc.__class__.__name__})"
            ) from None
        if stat.S_ISLNK(final_status.st_mode):
            raise UnsafePathError(f"{relpath}: symlink component {part!r}")
        if getattr(final_status, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
            raise UnsafePathError(f"{relpath}: reparse-point component {part!r}")
        if index < len(parts) - 1 and not stat.S_ISDIR(final_status.st_mode):
            raise UnsafePathError(f"{relpath}: non-directory component {part!r}")

    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise ScanReadError(
            f"{relpath}: cannot resolve ({exc.strerror or exc.__class__.__name__})"
        ) from None
    if not resolved.is_relative_to(base):
        raise UnsafePathError(f"{relpath}: resolves outside the repository root")
    if final_status is None or not stat.S_ISREG(final_status.st_mode):
        raise ScanReadError(f"{relpath}: not a regular file")
    return current


def _read_text(path: Path, relpath: str) -> str:
    """Open without following a final link component, or fail closed."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ScanReadError(
            f"{relpath}: cannot open ({exc.strerror or exc.__class__.__name__})"
        ) from None
    try:
        handle = os.fdopen(descriptor, "r", encoding="utf-8", errors="replace")
    except Exception:
        os.close(descriptor)
        raise
    with handle:
        try:
            return handle.read()
        except OSError as exc:
            raise ScanReadError(
                f"{relpath}: cannot read ({exc.strerror or exc.__class__.__name__})"
            ) from None


def _tracked_files(root: Path) -> list[str]:
    """Return the Git-tracked paths, or fail closed.

    There is deliberately **no filesystem fallback**: if ``git ls-files`` is
    unavailable or fails, walking the directory would read ignored/untracked
    files and ``.git`` — exactly what this tracked-tree-only gate must not do.
    Test fixtures must pass explicit ``paths=`` or initialize a Git repo.
    """

    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise ScanReadError(
            "git ls-files failed; refusing to traverse untracked files"
        ) from None
    return [
        name
        for name in proc.stdout.decode("utf-8", "surrogateescape").split("\0")
        if name
    ]


def scan_text(
    relpath: str,
    text: str,
    *,
    known_identifier_digests: frozenset[str] = KNOWN_PRIVATE_IDENTIFIER_SHA256,
) -> list[Finding]:
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
        if any(
            hashlib.sha256(token.lower().encode()).hexdigest()
            in known_identifier_digests
            for token in _TOKEN.findall(line)
        ):
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
        if (
            bearer
            and not _is_placeholder_value(bearer.group(1))
            and any(char.isdigit() for char in bearer.group(1))
        ):
            add("credential", lineno)

        device = _DEVICE.search(line)
        if device and not _is_placeholder_value(device.group(1)):
            add("device-reference", lineno)

        home = _HOME.search(line)
        if home and home.group(1).lower() not in ALLOWED_HOME_NAMES:
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


def scan_root(
    root: str | Path,
    *,
    paths: Iterable[str] | None = None,
    known_identifier_digests: frozenset[str] = KNOWN_PRIVATE_IDENTIFIER_SHA256,
) -> list[Finding]:
    """Scan ``root`` (tracked files by default) and return all findings.

    When ``paths`` is ``None``, selection is ``git ls-files`` only; a Git
    failure raises :class:`ScanReadError` instead of walking the filesystem.
    Explicit ``paths`` is for isolated test fixtures.  Raises
    :class:`UnsafePathError` or :class:`ScanReadError` instead of skipping a
    path it cannot validate or read.
    """

    try:
        base = Path(root).resolve(strict=True)
    except OSError as exc:
        raise ScanReadError(
            f"root: cannot resolve ({exc.strerror or exc.__class__.__name__})"
        ) from None
    if not base.is_dir():
        raise ScanReadError("root: not a directory")

    selected = list(paths) if paths is not None else _tracked_files(base)
    findings: list[Finding] = []
    for relpath in selected:
        safe_path = _safe_path(base, relpath)
        if _is_binary(relpath):
            continue
        text = _read_text(safe_path, relpath)
        findings.extend(
            scan_text(
                relpath.replace("\\", "/"),
                text,
                known_identifier_digests=known_identifier_digests,
            )
        )
    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.rule))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline public-tree safety scan")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--json", action="store_true", help="print machine-readable findings")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"public-tree safety scan: FAILED: root does not exist: {root}", file=sys.stderr)
        return 2

    try:
        findings = scan_root(root)
    except (UnsafePathError, ScanReadError) as exc:
        print(f"public-tree safety scan: FAILED: {exc}", file=sys.stderr)
        return 2

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
