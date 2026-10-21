"""把平台账户（``backend.auth.accounts``）并入 Registry 的受信主体目录。

Registry 只认一个 ``PrincipalDirectory`` 形状的对象（``lookup`` / ``require`` /
``approved_principals``）。历史上它是一份启动时写死的 Linux 用户表；平台账户由
管理员随时创建、停用，所以这里每次查询都读账户库，而不是在启动时拍快照。

* ``account:<id>`` → 一个 ``kind="account"`` 的 :class:`Principal`：``linux_user``
  是账户用户名（展示 / 路由标签），``uid`` 为空，``run_as`` 是管理员可选设定的
  「只能登记以该 Linux 用户运行的服务」；
* 停用的账户 → ``suspended``，不在 ``approved_principals`` 里；
* 其余主体原样交给静态目录。
"""

from __future__ import annotations

from ..registry.errors import Unauthenticated
from ..registry.identity import (
    PRINCIPAL_APPROVED,
    PRINCIPAL_KIND_ACCOUNT,
    PRINCIPAL_SUSPENDED,
    Principal,
    PrincipalDirectory,
    is_account_principal,
)


def account_principal(account) -> Principal:
    """``backend.auth.accounts.Account`` → Registry 主体。"""
    return Principal(
        principal=account.user.id,
        linux_user=account.username,
        uid=None,
        state=PRINCIPAL_APPROVED if account.user.is_active else PRINCIPAL_SUSPENDED,
        display_name=account.user.display_name,
        kind=PRINCIPAL_KIND_ACCOUNT,
        run_as=account.linux_user,
    )


class AccountAwareDirectory:
    """静态目录 + 平台账户库。与 :class:`PrincipalDirectory` 同一个形状。"""

    def __init__(self, static: PrincipalDirectory, accounts) -> None:
        self._static = static
        self._accounts = accounts

    def _account(self, principal: str) -> Principal | None:
        found = self._accounts.store.by_id(principal)
        return account_principal(found[0]) if found else None

    def lookup(self, principal: str) -> Principal | None:
        if is_account_principal(principal):
            return self._account(principal)
        return self._static.lookup(principal)

    def require(self, principal: str) -> Principal:
        found = self.lookup(principal)
        if found is None:
            raise Unauthenticated(f"principal {principal!r} is not in the trusted directory")
        return found

    def approved_principals(self) -> frozenset[str]:
        accounts = {
            account.user.id for account in self._accounts.list_accounts() if account.user.is_active
        }
        return self._static.approved_principals() | frozenset(accounts)

    def __len__(self) -> int:
        return len(self._static) + len(self._accounts.list_accounts())


__all__ = ["AccountAwareDirectory", "account_principal"]
