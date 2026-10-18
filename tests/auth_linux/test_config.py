"""Configuration loading: off by default, fail-closed when switched on."""

from __future__ import annotations

import unittest

from backend.auth_linux import errors
from backend.auth_linux.config import (
    DEFAULT_BROKER_TIMEOUT_SECONDS,
    DEFAULT_MAX_FAILURES,
    LinuxAuthConfig,
    from_environ,
)
from backend.auth_linux.policy import DEFAULT_MIN_HUMAN_UID

from . import support

BASE = {
    "LLM_LAB_LINUX_AUTH_ENABLED": "1",
    "LLM_LAB_LINUX_HOST_ID": support.SYNTHETIC_HOST_ID,
    "LLM_LAB_PAM_BROKER_SOCKET": "/run/llm-lab-portal/pam-broker.sock",
    "LLM_LAB_LINUX_ALLOWLIST_FILE": "/etc/llm-lab-portal/allowlist.json",
}


def environ(**overrides) -> dict[str, str]:
    merged = dict(BASE)
    merged.update(overrides)
    return {key: value for key, value in merged.items() if value is not None}


class DefaultOffTests(unittest.TestCase):
    def test_the_feature_is_off_when_nothing_is_set(self) -> None:
        config = from_environ({})
        self.assertFalse(config.enabled)
        self.assertIsNone(config.host_id)

    def test_an_explicit_false_leaves_it_off(self) -> None:
        for value in ("0", "false", "NO", "off", ""):
            with self.subTest(value=value):
                self.assertFalse(from_environ(environ(**{"LLM_LAB_LINUX_AUTH_ENABLED": value})).enabled)

    def test_a_disabled_config_needs_no_paths(self) -> None:
        config = from_environ({"LLM_LAB_LINUX_AUTH_ENABLED": "0"})
        self.assertFalse(config.enabled)
        self.assertIsNone(config.broker_socket_path)

    def test_a_disabled_config_refuses_to_be_used(self) -> None:
        with self.assertRaises(errors.NotConfigured):
            from_environ({}).require_enabled()


class EnabledTests(unittest.TestCase):
    def test_a_complete_configuration_loads(self) -> None:
        config = from_environ(environ())
        self.assertTrue(config.enabled)
        self.assertEqual(config.host_id, support.SYNTHETIC_HOST_ID)
        self.assertEqual(str(config.broker_socket_path), "/run/llm-lab-portal/pam-broker.sock")
        self.assertEqual(str(config.allowlist_path), "/etc/llm-lab-portal/allowlist.json")
        self.assertIsNone(config.admin_grant_path)
        self.assertEqual(config.broker_timeout_seconds, DEFAULT_BROKER_TIMEOUT_SECONDS)
        self.assertEqual(config.min_human_uid, DEFAULT_MIN_HUMAN_UID)
        self.assertEqual(config.max_failures, DEFAULT_MAX_FAILURES)
        self.assertEqual(config.require_enabled().host_id, support.SYNTHETIC_HOST_ID)

    def test_each_required_value_must_be_present(self) -> None:
        for missing in (
            "LLM_LAB_LINUX_HOST_ID",
            "LLM_LAB_PAM_BROKER_SOCKET",
            "LLM_LAB_LINUX_ALLOWLIST_FILE",
        ):
            with self.subTest(missing=missing):
                with self.assertRaises(errors.ConfigError):
                    from_environ(environ(**{missing: None}))
                with self.assertRaises(errors.ConfigError):
                    from_environ(environ(**{missing: "   "}))

    def test_paths_must_be_absolute(self) -> None:
        for key in ("LLM_LAB_PAM_BROKER_SOCKET", "LLM_LAB_LINUX_ALLOWLIST_FILE"):
            with self.subTest(key=key):
                with self.assertRaises(errors.ConfigError):
                    from_environ(environ(**{key: "relative/path"}))

    def test_a_host_identifier_must_look_like_one(self) -> None:
        for host in ("Dell-Test-01", "dell test 01", "-dell", "", "a" * 65, "dell/test-01", "dell:1"):
            with self.subTest(host=host):
                with self.assertRaises(errors.ConfigError):
                    from_environ(environ(**{"LLM_LAB_LINUX_HOST_ID": host}))

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        """A trailing newline from a config file is not a hostname."""
        config = from_environ(environ(**{"LLM_LAB_LINUX_HOST_ID": "dell-test-01\n"}))
        self.assertEqual(config.host_id, "dell-test-01")

    def test_a_boolean_typo_is_loud_rather_than_false(self) -> None:
        """A typo in a security switch must not read as "off"."""
        with self.assertRaises(errors.ConfigError):
            from_environ(environ(**{"LLM_LAB_LINUX_AUTH_ENABLED": "tru"}))

    def test_numeric_settings_are_validated(self) -> None:
        bad = {
            "LLM_LAB_LINUX_BROKER_TIMEOUT_SECONDS": ["zero", "-1", "0", "3600", "1e3"],
            "LLM_LAB_LINUX_MIN_UID": ["-1", "abc", "1.5"],
            "LLM_LAB_LINUX_MAX_FAILURES": ["0", "-5", "many"],
            "LLM_LAB_LINUX_WINDOW_SECONDS": ["0", "-1"],
            "LLM_LAB_LINUX_LOCKOUT_SECONDS": ["0", "-1"],
            "LLM_LAB_LINUX_POLICY_OWNER_UID": ["-1", "root"],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    with self.assertRaises(errors.ConfigError):
                        from_environ(environ(**{key: value}))

    def test_numeric_settings_accept_valid_values(self) -> None:
        config = from_environ(
            environ(
                **{
                    "LLM_LAB_LINUX_BROKER_TIMEOUT_SECONDS": "2.5",
                    "LLM_LAB_LINUX_MIN_UID": "1500",
                    "LLM_LAB_LINUX_MAX_FAILURES": "3",
                    "LLM_LAB_LINUX_WINDOW_SECONDS": "60",
                    "LLM_LAB_LINUX_LOCKOUT_SECONDS": "120",
                    "LLM_LAB_LINUX_POLICY_OWNER_UID": "4001",
                }
            )
        )
        self.assertEqual(config.broker_timeout_seconds, 2.5)
        self.assertEqual(config.min_human_uid, 1500)
        self.assertEqual(config.max_failures, 3)
        self.assertEqual(config.window_seconds, 60)
        self.assertEqual(config.lockout_seconds, 120)
        self.assertEqual(config.policy_owner_uid, 4001)

    def test_the_policy_owner_defaults_to_root(self) -> None:
        config = from_environ(environ())
        self.assertEqual(config.policy_owner_uid, 0)
        self.assertEqual(config.policy_parent_owner_uid, 0)

    def test_an_administrator_grant_path_is_optional(self) -> None:
        self.assertIsNone(from_environ(environ()).admin_grant_path)
        self.assertIsNone(
            from_environ(environ(**{"LLM_LAB_LINUX_ADMIN_GRANT_FILE": "  "})).admin_grant_path
        )
        config = from_environ(
            environ(**{"LLM_LAB_LINUX_ADMIN_GRANT_FILE": "/etc/llm-lab-portal/admins.json"})
        )
        self.assertEqual(str(config.admin_grant_path), "/etc/llm-lab-portal/admins.json")

    def test_the_grant_path_must_be_absolute_too(self) -> None:
        with self.assertRaises(errors.ConfigError):
            from_environ(environ(**{"LLM_LAB_LINUX_ADMIN_GRANT_FILE": "admins.json"}))


class ImmutabilityTests(unittest.TestCase):
    def test_the_configuration_cannot_be_edited_after_validation(self) -> None:
        config = from_environ(environ())
        with self.assertRaises(Exception):
            config.host_id = "somewhere-else"  # type: ignore[misc]

    def test_replace_still_produces_a_validated_shape(self) -> None:
        config = LinuxAuthConfig(enabled=True, host_id=support.SYNTHETIC_HOST_ID, min_human_uid=0)
        self.assertEqual(config.min_human_uid, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
