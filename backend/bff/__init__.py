"""Atelier 06 —— 面向 V6 管理后台的同源 HTTP BFF。

这个包是**集成层**，不是新系统。它做三件事，别的不做：

1. **把浏览器会话换成受信主体。** ``GET /api/v2/ops`` 的调用者身份只来自
   服务端会话记录（``backend.auth`` 的 session cookie），再由 Linux 账号目录
   补上 uid。请求头里的 ``X-UID`` / ``X-Admin`` 一律**拒绝**，不是「忽略」——
   见 :mod:`.identity`。
2. **把四个模块的 DTO 翻成前端已经写好的 ``portal-ops/1``。** 形状定义在
   ``web/js/features/contract.js``，本包不发明第二套；``backend.registry`` /
   ``backend.observability`` / ``backend.evaluation`` 各自的 DTO、可见性与
   口径都是唯一来源，本包只负责摆放（见 :mod:`.observability_adapter` 与
   :mod:`.evaluation_adapter`）。
3. **把 Linux 登录接进既有的会话实现。** ``backend.auth_linux`` 只产出
   ``VerifiedIdentity``；会话仍然由 ``backend.auth`` 的 ``SessionManager``
   签发，见 :mod:`.handoff`。

明确**不做**的事：不签发 API key、不接真实 Gateway、不改任何 V5/V6 视觉、
不引入除标准库以外的依赖、不部署。

**2026-10-17（Issue #79）新增的四条边界**，都是「含糊即失真」那类：

* ``capabilities`` —— 一个来源此刻对这个会话是 ``ready`` / ``stale`` /
  ``auth_required`` / ``not_configured``。在它之前，前两者的区别在前端长得一样。
* ``host_summary`` —— ``/api/infra/summary`` 的同源出口，**在服务端**跑
  ``backend.observability.host_projection`` 那道比前端更严的判定。
* ``atlas_adapter`` —— ``/api/atlas/calls`` 的同源出口；以前那条路径在同源 BFF
  上根本 404，而数据面明明在。
* 「客户端自选来源／目标／主体」是一个**封闭参数名的 400**（``.identity`` 的
  ``FORBIDDEN_PROXY_QUERY_KEYS``），不是每条处理器各自的记性。
"""

from __future__ import annotations

__all__ = [
    "app",
    "atlas_adapter",
    "capabilities",
    "evaluation_adapter",
    "handoff",
    "host_summary",
    "identity",
    "observability_adapter",
    "ops",
    "scopes",
    "sharing",
    "stacks",
]
