"""Linux 登录 ⇒ 平台会话：把 ``SessionHandoff`` 协议落到实处。

``backend/auth_linux`` 明确只到「产出 ``VerifiedIdentity``」为止，并声明会话签发
必须由 ``backend/auth`` 完成（见 ``handoff.py`` 的模块注释与 ``docs/linux-pam-login.md``）。
本模块就是那份协议在 Portal 侧的实现——**没有第二套会话实现**：

    LinuxAuthService.login(login_name, password, client_key)
        └─► VerifiedIdentity(login_name, uid, host_id, role, display_name)
              └─► LinuxAuthService.begin_session(identity, handoff)
                    └─► PortalSessionHandoff.start_session(UserDraft)
                          ├─► Database.ensure_linux_user(...)   # 只在首次写入 role
                          └─► SessionManager.create(user_id)    # 这里才产生 cookie

会话固定的防线（先撤销登录前那条会话）由调用方 :func:`sign_in_with_linux` 执行，
因为「登录前那条会话」是 **HTTP 层**才知道的事（它在 cookie jar 里），而这个
协议实现拿不到请求。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..auth import errors as auth_errors
from ..auth_linux import handoff as linux_handoff


@dataclass(frozen=True)
class LinuxSignIn:
    """一次成功登录的结果。

    协议把 ``start_session`` 的返回值声明为 opaque，所以这个形状是本实现自己的
    事：调用方只需要「谁登进来了」和「cookie 上该写什么」这两件事，两者都在这里，
    而密码不在。
    """

    user: object
    session_id: str
    csrf_token: str


class PortalSessionHandoff:
    """实现 ``backend.auth_linux.handoff.SessionHandoff``。

    会话**只**由 ``SessionManager.create`` 签发；本类不产生 cookie、不产生
    CSRF token，也不保存任何凭据。
    """

    def __init__(self, *, store, sessions) -> None:
        self._store = store
        self._sessions = sessions

    def start_session(self, draft: linux_handoff.UserDraft) -> LinuxSignIn:
        if not isinstance(draft, linux_handoff.UserDraft):
            raise ValueError("start_session expects a UserDraft")

        # 只在**首次**创建时写入 role / status；回来的登录只能改
        # display_name 与 last_login_at。因此这里传进去的 role 来自
        # PAM 策略，而不是请求 —— 且它一旦写下就不再被登录路径改写。
        user = self._store.ensure_linux_user(
            user_id=draft.user_id,
            external_id=draft.external_id,
            display_name=draft.display_name,
            role=draft.role,
        )
        issued = self._sessions.create(user.id)
        return LinuxSignIn(
            user=user, session_id=issued.session_id, csrf_token=issued.csrf_token
        )


def sign_in_with_linux(
    *,
    linux_service,
    handoff: PortalSessionHandoff,
    sessions,
    login_name: str,
    password: str,
    client_key: str,
    pre_login_session_id: str | None = None,
) -> object:
    """完整的一次 Linux 登录，返回新签发的 ``IssuedSession``。

    顺序是刻意的：

    1. **先**撤销登录前那条会话（会话固定防线）。放在登录成功之后、签发之前，
       这样一次失败的登录不会把现有会话踢掉；
    2. 校验 PAM（失败即抛 ``LinuxAuthError``，调用方翻成响应码，**不**签发）；
    3. 再交给 ``LinuxAuthService.begin_session`` → ``start_session`` 签发新会话。

    ``password`` 只在这一帧里存在，随后即随栈帧释放。它不会被写进日志、审计或
    任何返回值：``backend/auth_linux`` 全程不 import ``logging``，
    ``BrokerRequest.__repr__`` 把密码字段渲染成 ``<redacted>``，``AuditEvent``
    直接拒绝含 ``password``/``secret``/``hash`` 的字段名。测试
    ``test_no_credential_reaches_logs`` 会再验证一次这条性质在本层也成立。
    """
    identity = linux_service.login(login_name, password, client_key=client_key)

    if pre_login_session_id:
        try:
            sessions.revoke(pre_login_session_id)
        except auth_errors.AuthError:  # 已经失效的 cookie：无所谓，继续
            pass

    return linux_service.begin_session(identity, handoff)


__all__ = ["LinuxSignIn", "PortalSessionHandoff", "sign_in_with_linux"]
