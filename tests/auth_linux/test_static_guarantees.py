"""Guarantees about the source itself, checked mechanically.

Some promises about this package are easier to verify by reading its syntax than
by exercising its behaviour: that it never shells out, never grows a setuid
helper, never opens the shadow database, and never reaches into the auth
service's internals. A test that parses the modules keeps those promises from
decaying quietly.

These checks are about *code*. Prose in a docstring is allowed to name
``su`` or ``/etc/shadow`` while explaining why the module does not use them, so
the scan is over the abstract syntax tree rather than the raw text.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import backend.auth_linux as package

PACKAGE_DIR = pathlib.Path(package.__file__).parent
MODULES = sorted(path for path in PACKAGE_DIR.glob("*.py"))

# Importing any of these would change what this component *is*.
FORBIDDEN_IMPORTS = frozenset(
    {
        "subprocess",
        "ctypes",
        "cffi",
        "pty",
        "pexpect",
        "paramiko",
        "multiprocessing",
        "shlex",
    }
)

# Calling any of these would be the setuid helper, or the shell-out, that the
# design exists to avoid.
FORBIDDEN_ATTR_CALLS = frozenset(
    {
        "system",
        "popen",
        "execv",
        "execve",
        "execvp",
        "execl",
        "execle",
        "execlp",
        "spawnv",
        "spawnl",
        "spawnvp",
        "fork",
        "forkpty",
        "posix_spawn",
        "setuid",
        "setgid",
        "seteuid",
        "setegid",
        "setreuid",
        "setregid",
        "setresuid",
        "setresgid",
        "chroot",
        "unshare",
        "setgroups",
    }
)


def parse_all() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for path in MODULES:
        trees[path.name] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return trees


class SourceShapeTests(unittest.TestCase):
    def test_the_package_has_modules_to_check(self) -> None:
        self.assertGreaterEqual(len(MODULES), 10)

    def test_every_module_documents_itself(self) -> None:
        for name, tree in parse_all().items():
            with self.subTest(module=name):
                self.assertIsNotNone(ast.get_docstring(tree), f"{name} has no module docstring")


class NoProcessSpawnTests(unittest.TestCase):
    def test_nothing_imports_a_way_to_run_a_process(self) -> None:
        for name, tree in parse_all().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(module=name, imported=alias.name):
                            self.assertNotIn(alias.name.split(".")[0], FORBIDDEN_IMPORTS)
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    with self.subTest(module=name, imported=root):
                        self.assertNotIn(root, FORBIDDEN_IMPORTS)

    def test_nothing_calls_a_privilege_or_process_primitive(self) -> None:
        for name, tree in parse_all().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    with self.subTest(module=name, call=node.func.attr):
                        self.assertNotIn(node.func.attr, FORBIDDEN_ATTR_CALLS)

    def test_there_is_no_module_entry_point_that_does_anything(self) -> None:
        """A ``__main__`` block is where a helper grows into a command."""
        for name, tree in parse_all().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.If):
                    test = ast.dump(node.test)
                    with self.subTest(module=name):
                        self.assertNotIn("__main__", test)


class NoShadowAccessTests(unittest.TestCase):
    """The web process has no business reading the shadow database.

    The absence of ``pwd``-adjacent privileged reads is the reason the broker
    exists, so it is asserted rather than assumed.
    """

    def test_the_only_account_lookup_imported_is_pwd(self) -> None:
        imported: set[str] = set()
        for tree in parse_all().values():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add((node.module or "").split(".")[0])
        self.assertIn("pwd", imported)
        self.assertNotIn("spwd", imported)
        self.assertNotIn("crypt", imported)

    def test_no_shadow_path_is_used_as_a_string_in_code(self) -> None:
        """Docstrings may name it; a string *expression* would be a read."""
        for name, tree in parse_all().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if _is_docstring(node, tree):
                        continue
                    with self.subTest(module=name, value=node.value[:40]):
                        self.assertNotIn("/etc/shadow", node.value)
                        self.assertNotIn("/etc/sudoers", node.value)


def _is_docstring(node: ast.Constant, tree: ast.Module) -> bool:
    """True when ``node`` is the leading string statement of some block.

    A docstring is an ``Expr`` whose value is the ``Constant``, so the parent of
    the string is the expression statement, not the block that holds it. Missing
    that detail makes this helper silently return False for every docstring,
    which is exactly the kind of quiet no-op a scan must not have.
    """
    for parent in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(parent, field, None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and first.value is node:
                return True
    return False


class NoCouplingToTheAuthServiceTests(unittest.TestCase):
    def test_the_package_does_not_import_the_auth_service(self) -> None:
        """The handoff is a protocol, not an import, so the two can move apart."""
        for name, tree in parse_all().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    with self.subTest(module=name, imported=module):
                        self.assertFalse(module.startswith("backend.auth"))
                        self.assertNotEqual(module, "backend")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(module=name, imported=alias.name):
                            self.assertFalse(alias.name.startswith("backend."))


class SyntheticTestDataTests(unittest.TestCase):
    """The test suite must not contain a credential that could be real."""

    def test_the_shared_passwords_are_obviously_synthetic(self) -> None:
        from . import support

        for value in (support.GOOD_PASSWORD, support.WRONG_PASSWORD):
            with self.subTest(value=value):
                self.assertIn("synthetic", value)

    def test_no_test_module_reads_a_system_account_file(self) -> None:
        tests_dir = pathlib.Path(__file__).parent
        for path in sorted(tests_dir.glob("*.py")):
            if path.name == pathlib.Path(__file__).name:
                # This module names those paths in order to forbid them.
                continue
            text = path.read_text(encoding="utf-8")
            for forbidden in ("/etc/shadow", "/etc/pam.d", "/etc/sudoers", "/etc/passwd"):
                with self.subTest(module=path.name, path=forbidden):
                    self.assertNotIn(forbidden, text)

    def test_no_test_module_spawns_a_process(self) -> None:
        tests_dir = pathlib.Path(__file__).parent
        for path in sorted(tests_dir.glob("*.py")):
            if path.name == pathlib.Path(__file__).name:
                # This module lists those names in order to forbid them.
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(module=path.name, imported=alias.name):
                            self.assertNotIn(alias.name.split(".")[0], FORBIDDEN_IMPORTS)


class DefaultRefusalTests(unittest.TestCase):
    def test_the_only_shipped_backend_refuses_everything(self) -> None:
        from backend.auth_linux.pam_backend import (
            BACKEND_UNAVAILABLE,
            PamOutcome,
            build_pam_backend,
        )
        from . import support

        backend = build_pam_backend(BACKEND_UNAVAILABLE)
        for user in ("root", support.ADMIN_NAME, support.MEMBER_NAME):
            with self.subTest(user=user):
                result = backend.verify(user=user, password=support.GOOD_PASSWORD)
                self.assertEqual(result.outcome, PamOutcome.UNAVAILABLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
