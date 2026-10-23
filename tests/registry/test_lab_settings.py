"""§4.2：默认可见性只影响**之后**接入的服务。"""

from __future__ import annotations

import unittest

from backend.registry.lab_settings import (
    ALL,
    DEFAULT_VISIBILITY_KEY,
    PRIVATE,
    InvalidSetting,
    default_visibility,
    needs_route,
    set_default_visibility,
    visibility_for_new_service,
)


class FakeStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.writes: list[tuple[str, str, str | None]] = []

    def get_setting(self, key: str) -> str | None:
        return self.values.get(key)

    def set_setting(self, key: str, value: str, *, updated_by: str | None = None) -> None:
        self.values[key] = value
        self.writes.append((key, value, updated_by))


class DefaultVisibilityTests(unittest.TestCase):
    def test_unset_means_private(self):
        self.assertEqual(default_visibility(FakeStore()), PRIVATE)

    def test_a_corrupt_value_falls_back_to_private(self):
        """坏值退回安全方向：绝不能因为一行脏数据把服务变成公开。"""

        self.assertEqual(default_visibility(FakeStore({DEFAULT_VISIBILITY_KEY: "everyone"})), PRIVATE)

    def test_setting_records_who_changed_it(self):
        store = FakeStore()

        set_default_visibility(store, ALL, updated_by="admin")

        self.assertEqual(default_visibility(store), ALL)
        self.assertEqual(store.writes, [(DEFAULT_VISIBILITY_KEY, ALL, "admin")])

    def test_an_unknown_value_is_rejected_not_stored(self):
        store = FakeStore()
        with self.assertRaises(InvalidSetting):
            set_default_visibility(store, "public", updated_by="admin")
        self.assertEqual(store.writes, [])

    def test_the_documents_alias_all_is_stored_as_the_canonical_value(self):
        """§4.2 写的是 ``all``；库里只认 ``all_approved_platform_users``。

        两套词汇并存迟早会在某个 if 里对不上，所以在写入时就统一。
        """

        store = FakeStore()

        set_default_visibility(store, "all", updated_by="admin")

        self.assertEqual(store.values[DEFAULT_VISIBILITY_KEY], ALL)
        self.assertEqual(ALL, "all_approved_platform_users")


class VisibilityForNewServiceTests(unittest.TestCase):
    def test_owner_choice_beats_the_lab_default(self):
        store = FakeStore({DEFAULT_VISIBILITY_KEY: ALL})
        self.assertEqual(visibility_for_new_service(store, owner_choice=PRIVATE), PRIVATE)

    def test_lab_default_applies_when_the_owner_has_not_decided(self):
        store = FakeStore({DEFAULT_VISIBILITY_KEY: ALL})
        self.assertEqual(visibility_for_new_service(store, owner_choice=None), ALL)

    def test_a_bad_owner_choice_does_not_become_public(self):
        store = FakeStore({DEFAULT_VISIBILITY_KEY: PRIVATE})
        self.assertEqual(visibility_for_new_service(store, owner_choice="yes"), PRIVATE)

    def test_changing_the_default_does_not_touch_existing_services(self):
        """默认值只影响之后的接入：这就是「没有同时应用」的含义。"""

        store = FakeStore({DEFAULT_VISIBILITY_KEY: PRIVATE})
        existing = visibility_for_new_service(store)

        set_default_visibility(store, ALL, updated_by="admin")

        self.assertEqual(existing, PRIVATE)
        self.assertEqual(visibility_for_new_service(store), ALL)


class RouteTests(unittest.TestCase):
    def test_only_shared_services_need_a_route(self):
        self.assertTrue(needs_route(ALL))
        self.assertFalse(needs_route(PRIVATE))


if __name__ == "__main__":
    unittest.main()
