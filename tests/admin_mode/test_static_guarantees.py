"""Guarantees about the source, checked mechanically.

The promises that are easier to verify by reading syntax than by exercising
behaviour: that the module cannot log a password, that it never runs a process,
and that no real credential is hiding in the repository. A test that parses the
files keeps those from decaying quietly.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import backend.bff.admin_mode as admin_mode

MODULE = pathlib.Path(admin_mode.__file__)
REPO_ROOT = MODULE.parents[2]
FORBIDDEN_IMPORTS = frozenset({"subprocess", "ctypes", "pty", "pexpect", "paramiko"})

#: Names that must never be interpolated into a string. If one of these appears
#: inside an f-string or a `str()`/`+` concatenation, something is building a
#: message out of a secret.
SECRET_NAMES = frozenset({"password", "encoded", "new_password", "supplied", "secret"})


def parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class NoProcessSpawnTests(unittest.TestCase):
    def test_the_module_never_shells_out(self) -> None:
        for node in ast.walk(parse(MODULE)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    with self.subTest(imported=alias.name):
                        self.assertNotIn(alias.name.split(".")[0], FORBIDDEN_IMPORTS)
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                with self.subTest(imported=root):
                    self.assertNotIn(root, FORBIDDEN_IMPORTS)

    def test_the_module_never_calls_a_privilege_primitive(self) -> None:
        forbidden = {"system", "popen", "execv", "spawnv", "fork", "setuid", "setgid"}
        for node in ast.walk(parse(MODULE)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                with self.subTest(call=node.func.attr):
                    self.assertNotIn(node.func.attr, forbidden)


class NoLoggingTests(unittest.TestCase):
    def test_there_is_no_logging_module_in_the_path(self) -> None:
        """``logging`` is where a password ends up by accident.

        Nothing here logs; the audit trail is a table with no column for
        credential material, and the operator CLI prints only state words.
        """
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("import logging", source)

    def test_no_secret_is_interpolated_into_a_string(self) -> None:
        """Catches the realistic accident: an f-string in an error message."""
        offenders: list[str] = []
        for node in ast.walk(parse(MODULE)):
            if isinstance(node, ast.JoinedStr):
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and child.id in SECRET_NAMES:
                        offenders.append(f"{child.id} at line {child.lineno}")
        self.assertEqual(offenders, [], f"a secret is being formatted: {offenders}")

    def test_nothing_is_ever_printed_that_came_from_a_secret(self) -> None:
        """The operator CLI does print — to the terminal it was run at.

        What must never happen is a secret reaching that stream, so the rule is
        about the *argument*, not about `print` itself. The CLI's own output is
        state words and exit codes.
        """
        offenders: list[str] = []
        for node in ast.walk(parse(MODULE)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "print":
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and child.id in SECRET_NAMES:
                    offenders.append(f"{child.id} at line {child.lineno}")
        self.assertEqual(offenders, [], f"a secret is being printed: {offenders}")

    def test_the_password_is_not_embedded_in_an_exception_message(self) -> None:
        """``InvalidCredentials`` says nothing about what was supplied."""
        self.assertNotIn("password", admin_mode.InvalidCredentials.message.lower())
        self.assertNotIn("password", admin_mode.NotConfigured.message.lower())
        self.assertNotIn("password", admin_mode.RateLimited.message.lower())
        self.assertNotIn("password", admin_mode.Misconfigured.message.lower())


class NoRealCredentialTests(unittest.TestCase):
    """Nothing in this repository is, or looks like, a deployment credential."""

    def test_the_module_carries_no_literal_credential(self) -> None:
        """No committed hash, in any form.

        The check is on the *shape* of a string constant rather than on the
        algorithm prefixes: ``"$argon2id$"`` is a legitimate prefix constant,
        while ``"$scrypt$n=…$salt$digest"`` would be a credential in the source.
        """
        import re

        hash_shape = re.compile(r"^\$(scrypt|argon2id)\$.*\$.*\$")
        for node in ast.walk(parse(MODULE)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                with self.subTest(line=node.lineno):
                    self.assertIsNone(hash_shape.match(node.value), "a hash literal is committed")

    def test_the_fixture_passwords_are_obviously_synthetic(self) -> None:
        from . import support

        for value in (support.SHARED_PASSWORD, support.OTHER_SHARED_PASSWORD):
            with self.subTest(value=value):
                self.assertIn("synthetic", value)

    def test_no_test_module_reads_a_system_account_file(self) -> None:
        tests_dir = pathlib.Path(__file__).parent
        for path in sorted(tests_dir.glob("*.py")):
            if path.name == pathlib.Path(__file__).name:
                continue
            text = path.read_text(encoding="utf-8")
            for forbidden in ("/etc/shadow", "/etc/passwd", "/etc/pam.d", "/etc/sudoers"):
                with self.subTest(module=path.name, path=forbidden):
                    self.assertNotIn(forbidden, text)


class NoDefaultCredentialTests(unittest.TestCase):
    """The module must not be able to invent a password."""

    def test_there_is_no_default_password_parameter_anywhere(self) -> None:
        """``verify`` takes what it is given; it does not supply a fallback."""
        import inspect

        for name, member in inspect.getmembers(admin_mode.AdminCredentialStore, inspect.isfunction):
            with self.subTest(method=name):
                parameters = inspect.signature(member).parameters
                self.assertNotIn("default_password", parameters)
                for parameter in parameters.values():
                    self.assertNotIsInstance(
                        parameter.default,
                        str,
                        f"{name} has a string default that could be a password",
                    )

    def test_the_built_service_refuses_without_a_credential_file(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as tmp:
            service = admin_mode.build_service(
                admin_mode.AdminModeConfig(  # type: ignore[arg-type]
                    enabled=True,
                    credential_path=pathlib.Path(tmp) / admin_mode.DEFAULT_CREDENTIAL_FILE_NAME,
                    store_path=pathlib.Path(tmp) / admin_mode.DEFAULT_STORE_FILE_NAME,
                )
            )
            self.assertFalse(service.configured())


class DtoContractTests(unittest.TestCase):
    def test_the_status_payload_is_a_closed_shape(self) -> None:
        state = admin_mode.AdminModeState(configured=True, elevated=False, ttl_seconds=900)
        self.assertEqual(
            set(state.to_dict()), {"configured", "elevated", "ttl_seconds", "expires_at"}
        )

    def test_the_status_payload_never_carries_a_path_or_a_hash(self) -> None:
        state = admin_mode.AdminModeState(configured=True, elevated=True, ttl_seconds=900)
        rendered = repr(state.to_dict())
        for forbidden in ("/", "$", "scrypt"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
