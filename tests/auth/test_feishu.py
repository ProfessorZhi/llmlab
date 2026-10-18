"""Tests for backend.auth.feishu — the real provider, driven offline.

The provider is given a scripted transport, so these tests genuinely exercise
its URL building, request shapes, response parsing and tenant policy without a
single outbound packet. Endpoint constants are the official ones documented at

* https://open.feishu.cn/document/server-docs/authentication-management/access-token/create-2
* https://accounts.feishu.cn/open-apis/authen/v1/authorize (web login)
"""

import urllib.parse
import unittest

from backend.auth import errors, feishu
from tests.auth import support


def provider(transport, **overrides):
    kwargs = {
        "app_id": support.SYNTHETIC_APP_ID,
        "app_secret": support.SYNTHETIC_APP_SECRET,
        "redirect_uri": support.SYNTHETIC_REDIRECT,
        "tenant_allowlist": frozenset({support.SYNTHETIC_TENANT}),
        "transport": transport,
    }
    kwargs.update(overrides)
    return feishu.FeishuOAuthProvider(**kwargs)


class EndpointConstantTests(unittest.TestCase):
    def test_official_endpoints_are_used(self):
        self.assertEqual(
            feishu.AUTHORIZE_URL,
            "https://accounts.feishu.cn/open-apis/authen/v1/authorize",
        )
        self.assertEqual(
            feishu.APP_ACCESS_TOKEN_URL,
            "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        )
        self.assertEqual(
            feishu.USER_ACCESS_TOKEN_URL,
            "https://open.feishu.cn/open-apis/authen/v1/access_token",
        )
        self.assertEqual(
            feishu.USER_INFO_URL,
            "https://open.feishu.cn/open-apis/authen/v1/user_info",
        )

    def test_every_endpoint_is_https(self):
        for url in (
            feishu.AUTHORIZE_URL,
            feishu.APP_ACCESS_TOKEN_URL,
            feishu.USER_ACCESS_TOKEN_URL,
            feishu.USER_INFO_URL,
        ):
            with self.subTest(url=url):
                self.assertTrue(url.startswith("https://"), url)


class AuthorizationUrlTests(unittest.TestCase):
    def setUp(self):
        self.transport = support.FakeTransport()
        self.provider = provider(self.transport)

    def parse(self, url):
        self.assertTrue(url.startswith(feishu.AUTHORIZE_URL + "?"))
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

    def test_carries_client_id_redirect_and_state(self):
        query = self.parse(self.provider.authorization_url(state="state-abc"))
        self.assertEqual(query["client_id"], support.SYNTHETIC_APP_ID)
        self.assertEqual(query["redirect_uri"], support.SYNTHETIC_REDIRECT)
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["state"], "state-abc")

    def test_never_leaks_the_app_credential(self):
        url = self.provider.authorization_url(state="state-abc")
        self.assertNotIn(support.SYNTHETIC_APP_SECRET, url)

    def test_no_pkce_parameters_by_default(self):
        query = self.parse(self.provider.authorization_url(state="state-abc"))
        self.assertNotIn("code_challenge", query)

    def test_requests_pkce_only_when_the_provider_declares_support(self):
        self.assertFalse(self.provider.supports_pkce)
        with self.assertRaises(errors.ConfigError):
            self.provider.authorization_url(state="s", code_challenge="challenge")

    def test_attempting_to_enable_pkce_on_the_real_provider_fails_closed(self):
        """Feishu's documented endpoints take no code_verifier: refuse, do not downgrade."""
        with self.assertRaises(errors.ConfigError):
            provider(support.FakeTransport(), pkce_enabled=True)


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.transport = support.FakeTransport(support.success_script())
        self.provider = provider(self.transport)

    def test_successful_exchange_returns_the_identity(self):
        identity = self.provider.exchange_code(code="one-time-code")
        self.assertEqual(identity.open_id, support.SYNTHETIC_OPEN_ID)
        self.assertEqual(identity.union_id, support.SYNTHETIC_UNION_ID)
        self.assertEqual(identity.tenant_key, support.SYNTHETIC_TENANT)
        self.assertEqual(identity.display_name, "Synthetic Person")

    def test_identity_carries_no_credential(self):
        identity = self.provider.exchange_code(code="one-time-code")
        self.assertFalse(hasattr(identity, "access_token"))
        self.assertNotIn(
            "u-synthetic-user-token", repr(identity) + repr(getattr(identity, "__dict__", {}))
        )

    def test_app_credential_request_shape(self):
        self.provider.exchange_code(code="one-time-code")
        first = self.transport.requests[0]
        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["url"], feishu.APP_ACCESS_TOKEN_URL)
        self.assertEqual(first["payload"]["app_id"], support.SYNTHETIC_APP_ID)
        self.assertEqual(first["payload"]["app_secret"], support.SYNTHETIC_APP_SECRET)

    def test_code_request_shape(self):
        self.provider.exchange_code(code="one-time-code")
        second = self.transport.requests[1]
        self.assertEqual(second["method"], "POST")
        self.assertEqual(second["url"], feishu.USER_ACCESS_TOKEN_URL)
        self.assertEqual(second["payload"]["grant_type"], "authorization_code")
        self.assertEqual(second["payload"]["code"], "one-time-code")
        self.assertEqual(second["headers"]["Authorization"], "Bearer t-synthetic-app-token")

    def test_user_info_is_fetched_with_the_user_credential(self):
        self.provider.exchange_code(code="one-time-code")
        third = self.transport.requests[2]
        self.assertEqual(third["method"], "GET")
        self.assertEqual(third["url"], feishu.USER_INFO_URL)
        self.assertEqual(third["headers"]["Authorization"], "Bearer u-synthetic-user-token")

    def test_requests_are_bounded_by_a_timeout(self):
        self.provider.exchange_code(code="one-time-code")
        for request in self.transport.requests:
            with self.subTest(url=request["url"]):
                self.assertIsNotNone(request["timeout"])
                self.assertGreater(request["timeout"], 0)


class TenantPolicyTests(unittest.TestCase):
    def test_tenant_outside_the_allowlist_is_refused(self):
        transport = support.FakeTransport(
            support.success_script(tenant_key="tenant_not_allowed")
        )
        with self.assertRaises(errors.AccessDenied):
            provider(transport).exchange_code(code="c")

    def test_empty_allowlist_fails_closed_rather_than_accepting_everyone(self):
        transport = support.FakeTransport(support.success_script())
        with self.assertRaises(errors.NotConfigured):
            provider(transport, tenant_allowlist=frozenset()).exchange_code(code="c")

    def test_missing_tenant_key_is_refused(self):
        transport = support.FakeTransport(support.success_script(tenant_key=""))
        with self.assertRaises(errors.ProviderUnavailable):
            provider(transport).exchange_code(code="c")


class ProviderFailureTests(unittest.TestCase):
    def exchange_with(self, responses, **overrides):
        return provider(support.FakeTransport(responses), **overrides).exchange_code(code="c")

    def test_app_credential_error_is_mapped(self):
        with self.assertRaises(errors.ProviderUnavailable):
            self.exchange_with([support.error_response(10003, "invalid app credentials")])

    def test_invalid_code_from_the_provider_is_not_a_server_error(self):
        with self.assertRaises(errors.AccessDenied):
            self.exchange_with(
                [support.app_token_response(), support.error_response(20003, "code invalid")]
            )

    def test_frozen_or_resigned_user_is_mapped_to_access_denied(self):
        for code in (20021, 20022, 20023, 20008):
            with self.subTest(code=code):
                with self.assertRaises(errors.AccessDenied):
                    self.exchange_with(
                        [support.app_token_response(), support.error_response(code)]
                    )

    def test_http_500_is_a_provider_failure(self):
        with self.assertRaises(errors.ProviderUnavailable):
            self.exchange_with([support.raw_response(500, "boom")])

    def test_non_json_body_is_a_provider_failure(self):
        with self.assertRaises(errors.ProviderUnavailable):
            self.exchange_with([support.raw_response(200, "<html>not json</html>")])

    def test_nonzero_code_with_http_200_is_still_an_error(self):
        """Feishu returns many errors with HTTP 200; the body code must win."""
        with self.assertRaises(errors.ProviderUnavailable):
            self.exchange_with([support.raw_response(200, '{"code": 20050, "msg": "system"}')])

    def test_timeout_is_mapped(self):
        with self.assertRaises(errors.ProviderUnavailable):
            self.exchange_with([TimeoutError("timed out")])

    def test_connection_error_is_mapped(self):
        with self.assertRaises(errors.ProviderUnavailable):
            self.exchange_with([OSError("connection refused")])

    def test_missing_identity_fields_are_refused(self):
        body = {"code": 0, "msg": "ok", "data": {"name": "x", "tenant_key": support.SYNTHETIC_TENANT}}
        with self.assertRaises(errors.ProviderUnavailable):
            self.exchange_with([support.app_token_response(), (200, body)])

    def test_user_info_that_contradicts_the_token_is_refused(self):
        """A token/profile mismatch means something is very wrong: fail closed."""
        transport = support.FakeTransport(
            [
                support.app_token_response(),
                support.user_token_response(),
                support.user_info_response(open_id="ou_someone_else"),
            ]
        )
        with self.assertRaises(errors.ProviderUnavailable):
            provider(transport).exchange_code(code="c")

    def test_tenant_that_changes_between_calls_is_refused(self):
        transport = support.FakeTransport(
            [
                support.app_token_response(),
                support.user_token_response(),
                support.user_info_response(tenant_key="tenant_other"),
            ]
        )
        with self.assertRaises((errors.AccessDenied, errors.ProviderUnavailable)):
            provider(transport).exchange_code(code="c")


class ConfigurationTests(unittest.TestCase):
    def test_incomplete_provider_configuration_is_refused(self):
        for missing in ("app_id", "app_secret", "redirect_uri"):
            overrides = {
                "app_id": support.SYNTHETIC_APP_ID,
                "app_secret": support.SYNTHETIC_APP_SECRET,
                "redirect_uri": support.SYNTHETIC_REDIRECT,
            }
            overrides[missing] = None
            with self.subTest(missing=missing):
                with self.assertRaises(errors.ConfigError):
                    provider(
                        support.FakeTransport(),
                        tenant_allowlist=frozenset({support.SYNTHETIC_TENANT}),
                        **overrides,
                    )

    def test_redirect_uri_must_be_https(self):
        with self.assertRaises(errors.ConfigError):
            provider(support.FakeTransport(), redirect_uri="http://portal.example.com/cb")

    def test_provider_from_config_returns_none_when_unconfigured(self):
        import pathlib
        import shutil
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-prov-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = support.make_config(tmp)
        self.assertIsNone(feishu.provider_from_config(cfg, transport=support.FakeTransport()))

    def test_provider_from_config_builds_a_provider_when_configured(self):
        import pathlib
        import shutil
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-prov-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        transport = support.FakeTransport()
        cfg = support.make_config(tmp, **support.feishu_env())
        built = feishu.provider_from_config(cfg, transport=transport)
        self.assertIsInstance(built, feishu.FeishuOAuthProvider)
        self.assertIs(built.transport, transport)


if __name__ == "__main__":
    unittest.main()
