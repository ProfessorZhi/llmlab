"""Interactive bootstrap for the local administrator credential.

Run this **on the machine that owns the credential file**, by the account that
will serve the portal::

    python -m backend.auth.bootstrap init     --state-dir /srv/llm-lab/portal-auth
    python -m backend.auth.bootstrap status   --state-dir /srv/llm-lab/portal-auth
    python -m backend.auth.bootstrap rotate   --state-dir /srv/llm-lab/portal-auth
    python -m backend.auth.bootstrap disable  --state-dir /srv/llm-lab/portal-auth

Design constraints that this module deliberately enforces:

* The password is only ever read from an interactive prompt. There is no
  ``--password`` flag, no environment variable, and no stdin pipe. Anything that
  could end up in a shell history file, a process listing or a CI log is out.
* The new credential is written to a temporary file that is created ``0600`` and
  then atomically renamed, so a crash cannot leave a half-written hash behind.
* ``rotate`` and ``disable`` require the current password. ``--recover`` is the
  documented break-glass path for "the administrator is gone"; it assumes
  filesystem access, which is already equivalent to being able to rewrite the
  file, and is meant to be paired with the audit trail.
"""

from __future__ import annotations

import argparse
import getpass
import os
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from . import errors, hashing, local_admin
from .config import AuthConfig
from .local_admin import AdminCredentialStore

MIN_PASSWORD_LENGTH = 16

NEW_PROMPT = "New administrator password (input is not echoed): "
CONFIRM_PROMPT = "Repeat the new administrator password: "
CURRENT_PROMPT = "Current administrator password (input is not echoed): "

ReadPassword = Callable[[str], str]


def _interactive_reader(prompt: str) -> str:
    """Default reader. Only used when a human is at a terminal."""
    return getpass.getpass(prompt)


def validate_new_password(password: str, *, username: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise errors.InvalidRequest(
            detail=f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if not password.strip():
        raise errors.InvalidRequest(detail="password must not be blank")
    if password.strip().lower() == username.strip().lower():
        raise errors.InvalidRequest(detail="password must not be the administrator username")


def _store(cfg: AuthConfig) -> AdminCredentialStore:
    return AdminCredentialStore(cfg.admin_hash_file, username=cfg.admin_username)


def _read_confirmed(read_password: ReadPassword) -> str:
    first = read_password(NEW_PROMPT)
    second = read_password(CONFIRM_PROMPT)
    if first != second:
        raise errors.InvalidRequest(detail="the two entries did not match")
    return first


def _write_private(path: Path, content: str) -> None:
    """Atomically create ``path`` with mode 0600 and the given content."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows / odd FS
        pass

    handle, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".admin-hash-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        try:
            os.fchmod(handle, 0o600)
        except (AttributeError, OSError, NotImplementedError):  # pragma: no cover - Windows
            pass
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows / odd FS
        pass


def initialize_admin_password(
    cfg: AuthConfig,
    *,
    read_password: ReadPassword = _interactive_reader,
    force: bool = False,
) -> Path:
    """Create the administrator credential. Refuses to clobber an existing one."""
    store = _store(cfg)
    if not force and store.status()["state"] != local_admin.STATE_MISSING:
        raise errors.ConfigError(
            detail="an administrator credential already exists; use 'rotate' instead"
        )

    password = _read_confirmed(read_password)
    validate_new_password(password, username=cfg.admin_username)
    encoded = hashing.hash_password(password)
    _write_private(cfg.admin_hash_file, encoded + "\n")
    return cfg.admin_hash_file


def rotate_admin_password(
    cfg: AuthConfig,
    *,
    read_password: ReadPassword = _interactive_reader,
    recover: bool = False,
) -> Path:
    """Replace the administrator password, proving the current one by default."""
    store = _store(cfg)
    state = store.status()["state"]
    if state == local_admin.STATE_MISSING:
        raise errors.ConfigError(detail="no administrator credential to rotate; run 'init'")

    if not recover:
        current = read_password(CURRENT_PROMPT)
        try:
            accepted = store.authenticate(cfg.admin_username, current)
        except errors.AuthError as exc:
            raise errors.InvalidCredentials(detail=exc.detail) from None
        if not accepted:
            raise errors.InvalidCredentials(detail="current password rejected during rotation")

    password = _read_confirmed(read_password)
    validate_new_password(password, username=cfg.admin_username)
    encoded = hashing.hash_password(password)
    _write_private(cfg.admin_hash_file, encoded + "\n")
    return cfg.admin_hash_file


def disable_admin_login(
    cfg: AuthConfig,
    *,
    read_password: ReadPassword = _interactive_reader,
    recover: bool = False,
) -> Path:
    """Switch local administrator login off, leaving an explicit marker behind."""
    store = _store(cfg)
    if not recover:
        current = read_password(CURRENT_PROMPT)
        try:
            accepted = store.authenticate(cfg.admin_username, current)
        except errors.AuthError as exc:
            raise errors.InvalidCredentials(detail=exc.detail) from None
        if not accepted:
            raise errors.InvalidCredentials(detail="current password rejected during disable")

    _write_private(cfg.admin_hash_file, local_admin.DISABLED_SENTINEL + "\n")
    return cfg.admin_hash_file


def admin_credential_status(cfg: AuthConfig) -> dict[str, object]:
    return _store(cfg).status()


# --- command line -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.auth.bootstrap",
        description=(
            "Manage the local platform administrator credential. The password is "
            "requested interactively and is never accepted as an argument."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("init", "create the administrator credential (fails if one exists)"),
        ("rotate", "replace the administrator password"),
        ("disable", "disable local administrator login"),
        ("status", "report the credential state without revealing anything"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("--state-dir", help="directory holding the credential file")
        child.add_argument("--hash-file", help="explicit path to the credential file")
        child.add_argument(
            "--admin-username",
            help="administrator username; only 'admin' is accepted, any other value is refused",
        )
        if name == "init":
            child.add_argument("--force", action="store_true", help="overwrite an existing file")
        if name in ("rotate", "disable"):
            child.add_argument(
                "--recover",
                action="store_true",
                help="break-glass: skip the current-password check (needs filesystem access)",
            )
    return parser


def _config_from_args(args: argparse.Namespace) -> AuthConfig:
    environ = dict(os.environ)
    if args.state_dir:
        environ["LLM_LAB_AUTH_STATE_DIR"] = args.state_dir
    if args.hash_file:
        environ["LLM_LAB_ADMIN_HASH_FILE"] = args.hash_file
    if args.admin_username:
        environ["LLM_LAB_ADMIN_USERNAME"] = args.admin_username
    return AuthConfig.from_env(environ)


def main(argv: Sequence[str] | None = None, *, read_password: ReadPassword = _interactive_reader) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        cfg = _config_from_args(args)
        if args.command == "init":
            path = initialize_admin_password(cfg, read_password=read_password, force=args.force)
            print(f"Administrator credential written to {path} (mode 0600).")
        elif args.command == "rotate":
            path = rotate_admin_password(cfg, read_password=read_password, recover=args.recover)
            print(f"Administrator credential rotated at {path}.")
        elif args.command == "disable":
            path = disable_admin_login(cfg, read_password=read_password, recover=args.recover)
            print(f"Local administrator login disabled via {path}.")
        else:
            report = admin_credential_status(cfg)
            print(
                "state={state} algorithm={algorithm} needs_rehash={needs_rehash} path={path}".format(
                    **report
                )
            )
    except errors.AuthError as exc:
        print(f"error: {exc.message} ({exc.detail or exc.code})")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "admin_credential_status",
    "build_parser",
    "disable_admin_login",
    "initialize_admin_password",
    "main",
    "rotate_admin_password",
    "validate_new_password",
]
