"""Tests for backend.auth.config — defaults, parsing and fail-closed validation."""

import pathlib
import tempfile
import unittest

from backend.auth import errors
from backend.auth.config import AuthConfig

# Portable absolute path: the suite must pass on the offline Linux dev box and
# on a Windows workstation alike.
STATE_DIR = str(pathlib.Path(tempfile.gettempdir()) / "llm-lab-auth-tests")

MINIMAL = {"LLM_LAB_AUTH_STATE_DIR": STATE_DIR}

FEISHU = {
    "LLM_LAB_FEISHU_APP_ID": "cli_synthetic_app_id",
    "LLM_LAB_FEISHU_APP_SECRET": "synthetic-app-secret-value",
    "LLM_LAB_FEISHU_REDIRECT_URI": "https://portal.example.com/auth/feishu/callback",
    "LLM_LAB_FEISHU_TENANT_ALLOWLIST": "tenant_synthetic_a,tenant_synthetic_b",
}


def env(**overrides):
    merged = dict(MINIMAL)
    merged.update(overrides)
    return merged


def feishu_env(**overrides):
    merged = env(**FEISHU)
    merged.update(overrides)
    return merged


class MinimalConfigTests(unittest.TestCase):
    def test_minimal_env_loads(self):
        cfg = AuthConfig.from_env(env())
        self.assertEqual(cfg.admin_username, "admin")
        self.assertFalse(cfg.feishu_configured)

    def test_admin_username_is_hardcoded_to_admin(self):
        """The one local administrator is named 'admin' and cannot be renamed.

        This assertion used to read ``assertEqual(cfg.admin_username, "root")``,
        i.e. it asserted the *opposite* of what its own name claims: that an
        environment variable could mint a second local administrative identity
        beside the documented one. The fail-closed behaviour the product
        requires is asserted here instead — the override is refused, so the
        single-administrator invariant holds however the process is started.
        """
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env(env(LLM_LAB_ADMIN_USERNAME="root"))

    def test_an_admin_username_override_cannot_be_smuggled_in_through_replace(self):
        cfg = AuthConfig.from_env(env())
        with self.assertRaises(errors.ConfigError):
            cfg.replace(admin_username="operator")

    def test_the_documented_admin_name_is_accepted_explicitly(self):
        cfg = AuthConfig.from_env(env(LLM_LAB_ADMIN_USERNAME="admin"))
        self.assertEqual(cfg.admin_username, "admin")

    def test_cookie_flags_are_secure_by_default(self):
        cfg = AuthConfig.from_env(env())
        self.assertTrue(cfg.cookie_secure)
        self.assertTrue(cfg.cookie_httponly)
        self.assertEqual(cfg.cookie_samesite, "Lax")

    def test_admin_hash_file_defaults_beside_state_dir(self):
        cfg = AuthConfig.from_env(env())
        self.assertEqual(
            cfg.admin_hash_file, pathlib.Path(STATE_DIR) / "admin_password.hash"
        )

    def test_navigation_defaults_are_local_paths(self):
        cfg = AuthConfig.from_env(env())
        self.assertEqual(cfg.post_login_path, "/")
        self.assertEqual(cfg.login_error_path, "/login")


class FailClosedTests(unittest.TestCase):
    def test_state_dir_is_required(self):
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env({})

    def test_state_dir_must_be_absolute(self):
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env(env(LLM_LAB_AUTH_STATE_DIR="relative/dir"))

    def test_non_integer_timeout_is_refused(self):
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env(env(LLM_LAB_SESSION_IDLE_SECONDS="soon"))

    def test_non_positive_timeout_is_refused(self):
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env(env(LLM_LAB_SESSION_IDLE_SECONDS="0"))

    def test_partial_feishu_configuration_is_refused(self):
        for keep in ("LLM_LAB_FEISHU_APP_ID", "LLM_LAB_FEISHU_APP_SECRET",
                     "LLM_LAB_FEISHU_REDIRECT_URI"):
            partial = {k: v for k, v in FEISHU.items() if k != keep}
            with self.subTest(missing=keep):
                with self.assertRaises(errors.ConfigError):
                    AuthConfig.from_env(env(**partial))

    def test_configured_feishu_with_empty_tenant_allowlist_is_refused(self):
        """An empty allowlist must fail closed, not silently accept every tenant."""
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env(feishu_env(LLM_LAB_FEISHU_TENANT_ALLOWLIST=""))

    def test_plaintext_http_redirect_uri_is_refused(self):
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env(
                feishu_env(LLM_LAB_FEISHU_REDIRECT_URI="http://portal.example.com/auth/feishu/callback")
            )

    def test_redirect_uri_with_fragment_is_refused(self):
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env(
                feishu_env(LLM_LAB_FEISHU_REDIRECT_URI="https://portal.example.com/cb#frag")
            )

    def test_absolute_offsite_navigation_target_is_refused(self):
        for bad in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
            with self.subTest(bad=bad):
                with self.assertRaises(errors.ConfigError):
                    AuthConfig.from_env(env(LLM_LAB_POST_LOGIN_PATH=bad))

    def test_absolute_offsite_error_path_is_refused(self):
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env(env(LLM_LAB_LOGIN_ERROR_PATH="//evil.example/login"))


class FeishuConfigTests(unittest.TestCase):
    def test_complete_configuration_loads(self):
        cfg = AuthConfig.from_env(feishu_env())
        self.assertTrue(cfg.feishu_configured)
        self.assertEqual(
            cfg.feishu_tenant_allowlist,
            frozenset({"tenant_synthetic_a", "tenant_synthetic_b"}),
        )

    def test_trailing_and_duplicate_allowlist_entries_are_normalised(self):
        cfg = AuthConfig.from_env(
            feishu_env(LLM_LAB_FEISHU_TENANT_ALLOWLIST=" tenant_a , tenant_a ,tenant_b,")
        )
        self.assertEqual(cfg.feishu_tenant_allowlist, frozenset({"tenant_a", "tenant_b"}))

    def test_localhost_http_redirect_requires_explicit_optin(self):
        with self.assertRaises(errors.ConfigError):
            AuthConfig.from_env(
                feishu_env(LLM_LAB_FEISHU_REDIRECT_URI="http://localhost:8000/auth/feishu/callback")
            )
        cfg = AuthConfig.from_env(
            feishu_env(
                LLM_LAB_FEISHU_REDIRECT_URI="http://localhost:8000/auth/feishu/callback",
                LLM_LAB_ALLOW_INSECURE_REDIRECT_URI="1",
            )
        )
        self.assertTrue(cfg.allow_insecure_redirect_uri)

    def test_trusted_origins_are_parsed(self):
        cfg = AuthConfig.from_env(
            env(LLM_LAB_TRUSTED_ORIGINS="https://portal.example.com, http://localhost:8000")
        )
        self.assertEqual(
            cfg.trusted_origins,
            frozenset({"https://portal.example.com", "http://localhost:8000"}),
        )


class SecretHygieneTests(unittest.TestCase):
    def test_repr_does_not_leak_the_app_credential(self):
        cfg = AuthConfig.from_env(feishu_env())
        self.assertNotIn("synthetic-app-secret-value", repr(cfg))


if __name__ == "__main__":
    unittest.main()
