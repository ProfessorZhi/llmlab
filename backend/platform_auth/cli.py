"""Local operator CLI for platform accounts.

There is no default administrator and no generated password. The first account
is created by a human at the Dell terminal, typing the secret into
``getpass`` — it is never accepted as an argument (where it would land in shell
history, ``ps``, or a log), never read from a Linux account, and never printed.

The CLI writes only to the state directory it is given. It does not touch
``/etc/passwd``, PAM, SSH, systemd, or any service.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from .errors import PlatformAuthError
from .service import SCOPE_ADMIN, SCOPE_READ, PlatformAuthService
from .store import Store

#: Option spellings that are refused **before argparse sees them**. Accepting a
#: secret as an argument would put it in shell history, in ``ps``, and in the
#: process environment of every observer; argparse would then also echo it back
#: in an "unrecognized arguments" error. There is no supported way to pass one.
SECRET_OPTION_NAMES = frozenset({
    "password", "passwd", "pass", "pwd", "secret", "token", "api-key", "apikey", "p",
})

_SECRET_REFUSAL = (
    "error: platform secrets are not accepted as arguments; "
    "enter the password interactively when prompted."
)


class _SecretArgumentRefused(Exception):
    """Raised before parsing. Carries no value, on purpose."""


def _looks_like_secret_option(token: object) -> bool:
    if not isinstance(token, str) or not token.startswith("-"):
        return False
    name = token.split("=", 1)[0].lstrip("-").lower().replace("_", "-")
    if not name:
        return False
    return (
        name in SECRET_OPTION_NAMES
        or name.startswith("password")
        or name.startswith("secret")
        or name.startswith("token")
    )


class _SecretSafeParser(argparse.ArgumentParser):
    """argparse, minus the habit of echoing unrecognised values.

    ``parse_args`` formats unknown arguments into its error message, which is
    how a password supplied as ``--password <value>`` used to reach stderr.
    Values are never repeated here.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        if "unrecognized arguments" in message:
            message = "unrecognized arguments (values are not echoed)"
        super().error(message)


def parse_args(argv: list[str] | None = None):
    tokens = list(sys.argv[1:] if argv is None else argv)
    if any(_looks_like_secret_option(token) for token in tokens):
        print(_SECRET_REFUSAL, file=sys.stderr)
        raise _SecretArgumentRefused()
    return build_parser().parse_args(tokens)


def _read_password(prompt: str = "platform password: ") -> str:
    first = getpass.getpass(prompt)
    second = getpass.getpass("repeat password: ")
    if first != second:
        raise PlatformAuthError("passwords do not match")
    return first


def _service(args) -> PlatformAuthService:
    return PlatformAuthService(Store(args.state_dir))


def cmd_init_admin(args) -> int:
    service = _service(args)
    if service.store.count_users() != 0:
        print("refusing: platform accounts already exist; use create-user", file=sys.stderr)
        return 2
    password = _read_password()
    user = service.create_user(
        args.login, password, (SCOPE_READ, SCOPE_ADMIN),
        display_name=args.display_name,
    )
    print(json.dumps({"created": user["platform_user_id"],
                      "login_name": user["login_name"],
                      "scopes": user["scopes"]}))
    return 0


def cmd_create_user(args) -> int:
    service = _service(args)
    password = _read_password()
    user = service.create_user(
        args.login, password, tuple(args.scope), display_name=args.display_name
    )
    print(json.dumps({"created": user["platform_user_id"],
                      "login_name": user["login_name"],
                      "scopes": user["scopes"]}))
    return 0


def cmd_set_password(args) -> int:
    service = _service(args)
    password = _read_password()
    service.set_password(args.user_id, password)
    print(json.dumps({"password_changed": args.user_id, "sessions_revoked": True}))
    return 0


def cmd_disable(args) -> int:
    service = _service(args)
    service.set_disabled(args.user_id, True)
    print(json.dumps({"disabled": args.user_id, "sessions_revoked": True}))
    return 0


def cmd_enable(args) -> int:
    service = _service(args)
    service.set_disabled(args.user_id, False)
    print(json.dumps({"enabled": args.user_id}))
    return 0


def cmd_list(args) -> int:
    service = _service(args)
    print(json.dumps({"users": service.list_users()}, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = _SecretSafeParser(
        prog="python3 -m backend.platform_auth.cli",
        description=(
            "Manage independent platform accounts. Secrets are read from the "
            "terminal only; no --password option exists on purpose."
        ),
    )
    parser.add_argument("--state-dir", required=True,
                        help="owner-private directory (0700) holding platform_auth.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-admin", help="create the first platform administrator")
    init.add_argument("--login", required=True)
    init.add_argument("--display-name", default=None)
    init.set_defaults(func=cmd_init_admin)

    create = sub.add_parser("create-user", help="create a non-admin platform account")
    create.add_argument("--login", required=True)
    create.add_argument("--display-name", default=None)
    create.add_argument("--scope", action="append", default=[SCOPE_READ])
    create.set_defaults(func=cmd_create_user)

    rotate = sub.add_parser("set-password", help="rotate a password and revoke sessions")
    rotate.add_argument("--user-id", required=True)
    rotate.set_defaults(func=cmd_set_password)

    disable = sub.add_parser("disable", help="disable an account and revoke sessions")
    disable.add_argument("--user-id", required=True)
    disable.set_defaults(func=cmd_disable)

    enable = sub.add_parser("enable")
    enable.add_argument("--user-id", required=True)
    enable.set_defaults(func=cmd_enable)

    listing = sub.add_parser("list-users")
    listing.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except _SecretArgumentRefused:
        return 2
    try:
        return int(args.func(args))
    except PlatformAuthError as exc:
        print(f"error: {exc.code}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
