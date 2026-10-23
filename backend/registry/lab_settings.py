"""实验室级设置（§4.2 的 ``lab_settings`` 表 + 默认可见性）。

只有两条规则，但都容易写错：

* 默认值只决定**之后**接入的服务。管理员把默认值从 private 改成共享，不应该把
  昨天接入的一百个服务一夜之间变成公开——那是一次没人按过的共享按钮。
* 主人显式设置优先于默认值。默认值不是「覆盖」，是「尚未决定时用什么」。

可见性**只使用既有词表**（``models.VISIBILITY_*``），不另造一套 ``all``：设计
文档里的 ``all`` 在这里就是 ``all_approved_platform_users``，两套词汇并存迟早会
在某个 if 里对不上。

这里只依赖 store 的两个方法（``get_setting`` / ``set_setting``），便于用假 store 测。
"""

from __future__ import annotations

from typing import Protocol

from .models import (
    SETTING_DEFAULT_VISIBILITY,
    VISIBILITY_ALL_APPROVED,
    VISIBILITY_PRIVATE,
)

PRIVATE = VISIBILITY_PRIVATE
#: 设计文档 §4.2 的 ``all``。含义是「所有已审批的平台用户」，不是「任何人」。
ALL = VISIBILITY_ALL_APPROVED
VISIBILITIES = (PRIVATE, ALL)

DEFAULT_VISIBILITY_KEY = SETTING_DEFAULT_VISIBILITY


class SettingsStore(Protocol):
    def get_setting(self, key: str) -> str | None: ...

    def set_setting(self, key: str, value: str, *, updated_by: str | None = None) -> None: ...


class InvalidSetting(ValueError):
    """设置值不在允许范围内。"""


def default_visibility(store: SettingsStore) -> str:
    """读默认可见性；没有设置过、或库里的值坏了，都退回 ``private``。

    退回 private 是安全方向：一个坏值不该把服务变成公开的。
    """

    raw = store.get_setting(DEFAULT_VISIBILITY_KEY)
    return raw if raw in VISIBILITIES else PRIVATE


def normalize_visibility(value: object) -> str | None:
    """接受文档里的 ``all`` 与既有词表，统一成库里的值；其余一律 ``None``。"""

    if value in VISIBILITIES:
        return str(value)
    if value == "all":
        return ALL
    return None


def set_default_visibility(store: SettingsStore, value: str, *, updated_by: str | None = None) -> str:
    normalized = normalize_visibility(value)
    if normalized is None:
        raise InvalidSetting(f"default_visibility must be one of {VISIBILITIES}, got {value!r}")
    store.set_setting(DEFAULT_VISIBILITY_KEY, normalized, updated_by=updated_by)
    return normalized


def visibility_for_new_service(store: SettingsStore, *, owner_choice: str | None = None) -> str:
    """新接入服务的可见性。

    ``owner_choice`` 是主人显式设过的值；它优先于实验室默认值。坏值不静默降级成
    共享——退回默认值（默认值本身也只会是 private 或 all）。
    """

    chosen = normalize_visibility(owner_choice)
    if chosen is not None:
        return chosen
    return default_visibility(store)


def needs_route(visibility: str) -> bool:
    """只有共享的可见性需要网关路由；private 的服务仍可被主人直接按端口调用。"""

    return normalize_visibility(visibility) not in (None, PRIVATE)
