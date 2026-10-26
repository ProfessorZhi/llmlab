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


class AccountAwareDirectory(PrincipalDirectory):
    """静态目录 + 平台账户库。

    是 :class:`PrincipalDirectory` 的子类：Registry 的 ``resolve_requester``
    （token 通道）只认这个类型，所以平台账户的 CLI 令牌也走同一道判定。
    """

    def __init__(self, static: PrincipalDirectory, accounts) -> None:
        super().__init__(())
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

    def by_uid(self, uid: int) -> Principal | None:
        """``uid -> Principal`` 只可能命中静态 Linux 目录：平台账户没有 uid。

        必须显式委托。``super().__init__(())`` 把父类的 ``_by_uid`` 建成空表，
        而父类的 ``by_uid`` 只读那张表 —— 不覆盖的话，这台机器上**每一个**真实
        Linux 用户在这里都查不到，代理通道会以「uid 未映射」拒掉所有人。
        """
        return self._static.by_uid(uid)

    def uids(self) -> frozenset[int]:
        """与 :meth:`by_uid` 同一个理由：父类的表是空的。"""
        return self._static.uids()

    def account_for_linux_user(self, linux_user: str) -> Principal | None:
        """``linux_user -> account:<id>``：本机代理上报的模型归这个账户所有。

        约束与 ``ApprovalStore`` 同一个取向——归属只能有一个答案：

        * 只认**启用中**的账户（停用账户不该继续自动入账）；
        * 两个账户声明了同一个 Linux 用户时**不猜**，返回 ``None``（宁可不登记，
          也不能把模型记到错的人名下）；
        * ``linux_user`` 在账户表里是标签而不是凭据，所以这里不做任何身份推断，
          只回答「表里有没有人认领这个名字」。
        """

        if not isinstance(linux_user, str) or not linux_user:
            return None
        matches = [
            account
            for account in self._accounts.list_accounts()
            if account.linux_user == linux_user and account.user.is_active
        ]
        if len(matches) != 1:
            return None
        return account_principal(matches[0])

    def approved_principals(self) -> frozenset[str]:
        accounts = {
            account.user.id for account in self._accounts.list_accounts() if account.user.is_active
        }
        return self._static.approved_principals() | frozenset(accounts)

    def __len__(self) -> int:
        return len(self._static) + len(self._accounts.list_accounts())


__all__ = ["AccountAwareDirectory", "account_principal"]
