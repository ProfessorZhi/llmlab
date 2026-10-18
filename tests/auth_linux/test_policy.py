"""Allowlist, administrator grant, and the account policy that reads them."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.auth_linux import errors
from backend.auth_linux.policy import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    AccountPolicy,
    AdminGrant,
    Allowlist,
    load_admin_grant,
    load_allowlist,
    no_admin_grant,
    parse_admin_grant,
    parse_allowlist,
)
from backend.auth_linux.reasons import DenialReason
from backend.auth_linux.securefile import FileFacts

from . import support

HOST = support.SYNTHETIC_HOST_ID


def allowlist_document(names, **extra) -> str:
    payload = {"version": 1, "allowed_users": list(names)}
    payload.update(extra)
    return json.dumps(payload)


def grant_document(entries, *, host_id: str = HOST, **extra) -> str:
    payload = {"version": 1, "host_id": host_id, "platform_admins": list(entries)}
    payload.update(extra)
    return json.dumps(payload)


class AllowlistParsingTests(unittest.TestCase):
    def test_a_well_formed_allowlist_loads(self) -> None:
        allowlist = parse_allowlist(allowlist_document(["carol", "alice", "dave"]))
        self.assertEqual(allowlist.sorted_users(), ["alice", "carol", "dave"])
        self.assertIn("carol", allowlist)
        self.assertNotIn("root", allowlist)

    def test_duplicates_collapse_and_order_is_deterministic(self) -> None:
        allowlist = parse_allowlist(allowlist_document(["alice", "carol", "alice"]))
        self.assertEqual(allowlist.sorted_users(), ["alice", "carol"])

    def test_unknown_keys_are_refused_rather_than_ignored(self) -> None:
        with self.assertRaises(errors.ConfigError):
            parse_allowlist(allowlist_document(["carol"], note="temporary"))

    def test_an_alias_table_does_not_work(self) -> None:
        """The load-bearing test for "dana is not bob".

        If a document could express ``dana -> bob``, then whoever writes the
        allowlist could bind a person to an account that is not theirs, and the
        login would look legitimate. There is no such field, and a document that
        tries to add one fails to load instead of silently dropping it.
        """
        document = allowlist_document(["bob"], aliases={support.UNBOUND_NAME: "bob"})
        with self.assertRaises(errors.ConfigError):
            parse_allowlist(document)

    def test_a_listed_name_is_not_reachable_under_another_spelling(self) -> None:
        allowlist = parse_allowlist(allowlist_document(["bob"]))
        self.assertIn("bob", allowlist)
        self.assertNotIn(support.UNBOUND_NAME, allowlist)
        self.assertNotIn("Bob", allowlist)

    def test_an_empty_allowlist_refuses_to_load(self) -> None:
        with self.assertRaises(errors.ConfigError):
            parse_allowlist(allowlist_document([]))

    def test_a_missing_version_or_a_wrong_one_is_refused(self) -> None:
        for document in (
            json.dumps({"allowed_users": ["carol"]}),
            json.dumps({"version": 2, "allowed_users": ["carol"]}),
            json.dumps({"version": "1", "allowed_users": ["carol"]}),
        ):
            with self.subTest(document=document):
                with self.assertRaises(errors.ConfigError):
                    parse_allowlist(document)

    def test_a_non_list_or_malformed_document_is_refused(self) -> None:
        for document in (
            json.dumps({"version": 1, "allowed_users": "carol"}),
            json.dumps({"version": 1, "allowed_users": [1, 2]}),
            json.dumps({"version": 1, "allowed_users": [None]}),
            json.dumps([1, 2, 3]),
            "not json at all",
            "",
        ):
            with self.subTest(document=document):
                with self.assertRaises(errors.ConfigError):
                    parse_allowlist(document)

    def test_a_name_that_could_not_be_a_login_name_is_refused(self) -> None:
        for name in ("Root", "root\n", "-carol", "1carol", "carol/../root", "josé"):
            with self.subTest(name=name):
                with self.assertRaises(errors.ConfigError):
                    parse_allowlist(allowlist_document([name]))

    def test_containment_rejects_non_strings(self) -> None:
        allowlist = parse_allowlist(allowlist_document(["carol"]))
        for bad in (None, 1, b"carol", ["carol"]):
            with self.subTest(value=bad):
                self.assertNotIn(bad, allowlist)


class AdminGrantParsingTests(unittest.TestCase):
    def test_a_well_formed_grant_loads(self) -> None:
        grant = parse_admin_grant(
            grant_document([{"uid": support.ADMIN_UID, "name": support.ADMIN_NAME}]), host_id=HOST
        )
        self.assertTrue(grant.grants_admin(uid=support.ADMIN_UID, name=support.ADMIN_NAME))
        self.assertEqual(grant.sorted_entries(), [(support.ADMIN_UID, support.ADMIN_NAME)])

    def test_an_empty_grant_is_valid_and_grants_nothing(self) -> None:
        grant = parse_admin_grant(grant_document([]), host_id=HOST)
        self.assertFalse(grant.grants_admin(uid=support.ADMIN_UID, name=support.ADMIN_NAME))
        self.assertEqual(grant.entries, frozenset())

    def test_absence_of_a_grant_file_grants_nothing(self) -> None:
        grant = no_admin_grant(HOST)
        self.assertFalse(grant.grants_admin(uid=0, name="root"))
        self.assertFalse(grant.grants_admin(uid=support.ADMIN_UID, name=support.ADMIN_NAME))

    def test_a_grant_for_another_host_is_refused(self) -> None:
        """A promoted account on machine A must not be promoted on machine B."""
        with self.assertRaises(errors.ConfigError):
            parse_admin_grant(
                grant_document([{"uid": 4000, "name": "carol"}], host_id="some-other-host"),
                host_id=HOST,
            )

    def test_entry_shape_is_strict(self) -> None:
        bad_entries = [
            [{"uid": "4000", "name": "carol"}],
            [{"uid": True, "name": "carol"}],
            [{"uid": -1, "name": "carol"}],
            [{"uid": 4000, "name": "Root"}],
            [{"uid": 4000}],
            [{"name": "carol"}],
            [{"uid": 4000, "name": "carol", "role": "platform_admin"}],
            ["carol"],
            [None],
        ]
        for entries in bad_entries:
            with self.subTest(entries=entries):
                with self.assertRaises(errors.ConfigError):
                    parse_admin_grant(grant_document(entries), host_id=HOST)

    def test_unknown_top_level_keys_are_refused(self) -> None:
        with self.assertRaises(errors.ConfigError):
            parse_admin_grant(
                grant_document([{"uid": 4000, "name": "carol"}], default_role=ROLE_ADMIN),
                host_id=HOST,
            )

    def test_both_uid_and_name_must_match_to_grant(self) -> None:
        grant = parse_admin_grant(
            grant_document([{"uid": support.ADMIN_UID, "name": support.ADMIN_NAME}]), host_id=HOST
        )
        self.assertFalse(grant.grants_admin(uid=support.ADMIN_UID, name="someone-else"))
        self.assertFalse(grant.grants_admin(uid=999, name=support.ADMIN_NAME))


class PolicyFileLoadingTests(unittest.TestCase):
    """Permission enforcement, with the running user standing in for root."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.owner = os.getuid()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str, *, mode: int = 0o600) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_a_root_owned_style_file_loads(self) -> None:
        path = self.write("allow.json", allowlist_document(["carol"]))
        allowlist = load_allowlist(
            path, required_owner_uid=self.owner, required_parent_owner_uid=self.owner
        )
        self.assertIn("carol", allowlist)

    def test_a_world_writable_file_is_refused(self) -> None:
        path = self.write("allow.json", allowlist_document(["carol"]), mode=0o666)
        with self.assertRaises(errors.ConfigError):
            load_allowlist(
                path, required_owner_uid=self.owner, required_parent_owner_uid=self.owner
            )

    def test_a_group_writable_file_is_refused(self) -> None:
        path = self.write("allow.json", allowlist_document(["carol"]), mode=0o664)
        with self.assertRaises(errors.ConfigError):
            load_allowlist(
                path, required_owner_uid=self.owner, required_parent_owner_uid=self.owner
            )

    def test_a_missing_file_is_refused(self) -> None:
        with self.assertRaises(errors.ConfigError):
            load_allowlist(
                self.dir / "absent.json",
                required_owner_uid=self.owner,
                required_parent_owner_uid=self.owner,
            )

    def test_the_wrong_owner_is_refused(self) -> None:
        path = self.write("allow.json", allowlist_document(["carol"]))
        with self.assertRaises(errors.ConfigError):
            load_allowlist(
                path,
                required_owner_uid=self.owner + 1,
                required_parent_owner_uid=self.owner,
            )

    def test_a_writable_parent_directory_is_refused(self) -> None:
        """A root-owned file in a user-writable directory is not protected."""
        path = self.write("allow.json", allowlist_document(["carol"]))
        os.chmod(self.dir, 0o777)
        try:
            with self.assertRaises(errors.ConfigError):
                load_allowlist(
                    path, required_owner_uid=self.owner, required_parent_owner_uid=self.owner
                )
        finally:
            os.chmod(self.dir, 0o700)

    def test_an_oversized_file_is_refused(self) -> None:
        huge = {"version": 1, "allowed_users": ["carol"], "padding": "x" * (70 * 1024)}
        path = self.write("allow.json", json.dumps(huge))
        with self.assertRaises(errors.ConfigError):
            load_allowlist(
                path, required_owner_uid=self.owner, required_parent_owner_uid=self.owner
            )

    def test_a_directory_where_a_file_is_expected_is_refused(self) -> None:
        nested = self.dir / "adir"
        nested.mkdir()
        with self.assertRaises(errors.ConfigError):
            load_allowlist(
                nested, required_owner_uid=self.owner, required_parent_owner_uid=self.owner
            )

    def test_the_grant_loader_applies_the_same_rules(self) -> None:
        path = self.write(
            "grant.json",
            grant_document([{"uid": support.ADMIN_UID, "name": support.ADMIN_NAME}]),
        )
        grant = load_admin_grant(
            path,
            host_id=HOST,
            required_owner_uid=self.owner,
            required_parent_owner_uid=self.owner,
        )
        self.assertTrue(grant.grants_admin(uid=support.ADMIN_UID, name=support.ADMIN_NAME))

        os.chmod(path, 0o646)
        with self.assertRaises(errors.ConfigError):
            load_admin_grant(
                path,
                host_id=HOST,
                required_owner_uid=self.owner,
                required_parent_owner_uid=self.owner,
            )


class InjectedProbeTests(unittest.TestCase):
    """The probe is injectable, so the checks can be exercised anywhere."""

    def test_an_unknown_owner_probe_refuses(self) -> None:
        def probe(path: Path) -> FileFacts:
            return FileFacts(
                exists=True, is_regular=True, mode=0o600, size=10, uid=None, gid=None
            )

        with self.assertRaises(errors.ConfigError):
            load_allowlist(
                "/nowhere/allow.json",
                required_owner_uid=0,
                required_parent_owner_uid=None,
                fs_probe=probe,
            )

    def test_a_non_regular_file_is_refused(self) -> None:
        def probe(path: Path) -> FileFacts:
            if path.name == "allow.json":
                return FileFacts(
                    exists=True, is_regular=False, mode=0o600, size=10, uid=0, gid=0
                )
            return FileFacts(
                exists=True, is_regular=True, mode=0o700, size=10, uid=0, gid=0
            )

        with self.assertRaises(errors.ConfigError):
            load_allowlist(
                "/etc/llm-lab/allow.json",
                required_owner_uid=0,
                required_parent_owner_uid=0,
                fs_probe=probe,
            )


class AccountPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = support.default_policy()

    def evaluate(self, name: str, uid: int, shell: str = "/bin/bash"):
        return self.policy.evaluate(support.make_account(name, uid, shell=shell))

    def test_an_allowlisted_human_account_is_admitted_as_a_member(self) -> None:
        decision = self.evaluate(support.MEMBER_NAME, support.MEMBER_UID)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.role, ROLE_MEMBER)
        self.assertIsNone(decision.reason)

    def test_a_granted_account_is_admitted_as_a_platform_administrator(self) -> None:
        decision = self.evaluate(support.ADMIN_NAME, support.ADMIN_UID)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.role, ROLE_ADMIN)

    def test_an_allowlisted_account_without_a_grant_is_only_a_member(self) -> None:
        policy = AccountPolicy(
            allowlist=Allowlist(frozenset({support.MEMBER_NAME})),
            host_id=HOST,
            admin_grant=support.default_admin_grant(),
        )
        decision = policy.evaluate(support.make_account(support.MEMBER_NAME, support.MEMBER_UID))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.role, ROLE_MEMBER)

    def test_root_is_refused_even_when_the_allowlist_names_it(self) -> None:
        """The check that must survive an operator mistake."""
        policy = AccountPolicy(
            allowlist=Allowlist(frozenset({"root"})),
            host_id=HOST,
            admin_grant=AdminGrant(host_id=HOST, entries=frozenset({(0, "root")})),
        )
        decision = policy.evaluate(support.make_account("root", 0))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DenialReason.ROOT_FORBIDDEN)
        self.assertIsNone(decision.role)

    def test_system_accounts_are_refused_on_uid(self) -> None:
        # A name that is not on the explicit deny list, so the UID floor is the
        # only thing that can refuse it.
        policy = AccountPolicy(
            allowlist=Allowlist(frozenset({"lowuid-svc"})), host_id=HOST, min_human_uid=1000
        )
        decision = policy.evaluate(support.make_account("lowuid-svc", 1))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DenialReason.SYSTEM_ACCOUNT)

    def test_a_known_service_name_is_refused_explicitly(self) -> None:
        """Deny-listed names are refused even if the UID floor would allow them."""
        policy = AccountPolicy(
            allowlist=Allowlist(frozenset({"daemon"})), host_id=HOST, min_human_uid=0
        )
        decision = policy.evaluate(support.make_account("daemon", 5000))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DenialReason.EXPLICITLY_DENIED)

    def test_a_non_login_shell_is_refused(self) -> None:
        for shell in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false", ""):
            with self.subTest(shell=shell):
                decision = self.evaluate(support.MEMBER_NAME, support.MEMBER_UID, shell=shell)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, DenialReason.NON_LOGIN_SHELL)

    def test_an_account_outside_the_allowlist_is_refused(self) -> None:
        decision = self.evaluate(support.UNBOUND_NAME, 1100)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DenialReason.NOT_ALLOWLISTED)

    def test_a_renamed_account_loses_its_administrator_role(self) -> None:
        """The grant names a name as well as a UID, so a rename drops it."""
        decision = self.evaluate("carol-renamed", support.ADMIN_UID)
        # Not allowlisted under the new name, so it is refused outright.
        self.assertFalse(decision.allowed)

        policy = AccountPolicy(
            allowlist=Allowlist(frozenset({"carol-renamed"})),
            host_id=HOST,
            admin_grant=support.default_admin_grant(),
        )
        decision = policy.evaluate(support.make_account("carol-renamed", support.ADMIN_UID))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.role, ROLE_MEMBER)

    def test_a_recycled_uid_does_not_inherit_the_administrator_role(self) -> None:
        policy = AccountPolicy(
            allowlist=Allowlist(frozenset({"newperson"})),
            host_id=HOST,
            admin_grant=support.default_admin_grant(),
        )
        decision = policy.evaluate(support.make_account("newperson", support.ADMIN_UID))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.role, ROLE_MEMBER)

    def test_protective_checks_outrank_the_allowlist(self) -> None:
        """Order is asserted, not assumed."""
        policy = AccountPolicy(
            allowlist=Allowlist(frozenset({"root", "lowuid-svc", "svc"})),
            host_id=HOST,
            min_human_uid=1000,
        )
        cases = {
            ("root", 0): DenialReason.ROOT_FORBIDDEN,
            ("lowuid-svc", 1): DenialReason.SYSTEM_ACCOUNT,
            ("svc", 1500): DenialReason.NON_LOGIN_SHELL,
        }
        for (name, uid), reason in cases.items():
            shell = "/usr/sbin/nologin" if name == "svc" else "/bin/bash"
            with self.subTest(name=name):
                decision = policy.evaluate(support.make_account(name, uid, shell=shell))
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, reason)

    def test_a_denied_decision_never_carries_a_role(self) -> None:
        for name, uid, shell in (
            ("root", 0, "/bin/bash"),
            ("lowuid-svc", 1, "/bin/bash"),
            ("alice", 4001, "/usr/sbin/nologin"),
            ("stranger", 1200, "/bin/bash"),
        ):
            with self.subTest(name=name):
                decision = self.policy.evaluate(support.make_account(name, uid, shell=shell))
                self.assertFalse(decision.allowed)
                self.assertIsNone(decision.role)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
