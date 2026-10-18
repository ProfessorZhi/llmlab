"""``/api/infra/summary`` 的同源只读接线：固定来源 + 服务端严格校验。

在这条路由存在之前，``/api/infra/summary`` 只有 Node 侧的预览实现
（``web/tools/serve.mjs``，``ATELIER_INFRA_READONLY=1`` 时才开），Python BFF 没有
挂载它。于是「主机读数」这件事在浏览器上有两个可能的来源，而其中只有一个是
同源、按会话鉴权的。

**把这条路由接进来的价值不在转发，而在校验。** ``backend/observability/host_projection``
已经有一份比前端更严的判定器（``source`` 必须逐字是 ``prometheus-readonly``、
``synthetic`` 必须是 JSON ``false``、``version`` 必须是 1、每个 ``sample_at`` 必须
带时区且落在 90 秒窗口内），但在这条路由之前它只被报告自检调用过，**没有一条
面向浏览器的路径**在把投影下发之前用它判一次。前端那一行
``source === ... && version === 1 && synthetic === false`` 是一条客户端防线：
一份被中间人改写、由旧版本工具生成、或者干脆伪造的响应，只要能过那行 ``if``，
就会被画成 LIVE。

本层把那道判定搬到**下发之前**，并且：

**来源是注入的，不是请求里的。** :attr:`HostSummaryStack.read` 是一个零参数的
callable。路由上**没有** ``?url=``、``?promql=``、``?path=``、``?target=``、
``?port=`` 这类参数 —— 不是「读了不用」，是根本没有这条代码路径。一个开放的
PromQL / URL 反代口就是一张直通内网的通行证。

**读不到不是 0。** ``read()`` 返回 ``None``（还没取到 / 这次没取到）与返回一份
内容为空的投影是两件事：前者以 503 收场，后者交给判定器逐条降级成 UNKNOWN。
两条路都不会产出一个看起来正常的数字。

**不自己缓存。** 缓存策略是部署决策（``backend.observability.host_refresh`` 就是
为它写的），不是 BFF 的。本层每次问 ``read()``；生产里注入的应当是那个已经有
刷新周期的读取器，而不是一个裸的 HTTP 客户端。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ..observability import errors as observability_errors
from ..observability import identity as observability_identity
from ..observability.host_projection import (
    DEFAULT_FUTURE_TOLERANCE_S,
    DEFAULT_MAX_SAMPLE_AGE_S,
    PROJECTION_PATH,
    HostSummaryVerdict,
    parse_instant,
    validate_host_summary,
)
from . import scopes as bff_scopes

#: 本部署没有装配主机投影来源。
REASON_HOST_SUMMARY_NOT_ATTACHED = "host_summary_adapter_not_attached"

#: DTO 上的 schema 标记。它描述**本层包出来的那一层**，不是上游投影的
#: ``version``（那个在 ``verdict.version`` 里，由判定器逐字校验）。
SCHEMA_VERSION = "portal-host-summary/1"


@dataclass(frozen=True)
class HostSummaryStack:
    """接线主机投影所需的一切。

    ``read()`` **不接受任何参数**，这是这一层的安全边界本身：一个能收参数的
    ``read`` 迟早会有人从请求里给它填一个。它返回一份**已经解析好的**投影对象
    （或 ``None`` 表示这次没读到）——网络、超时、重试都在它背后，不在本层。
    """

    read: Callable[[], object] = lambda: None
    max_sample_age_s: int = DEFAULT_MAX_SAMPLE_AGE_S
    future_tolerance_s: int = DEFAULT_FUTURE_TOLERANCE_S

    def __post_init__(self) -> None:
        if not callable(self.read):
            raise ValueError("read must be callable and take no request parameters")
        for name in ("max_sample_age_s", "future_tolerance_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


class ModuleHostSummaryAdapter:
    """把主机投影接成 BFF 的 Host summary 来源。"""

    name = "host_summary"
    attached = True

    def __init__(
        self,
        stack: HostSummaryStack,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.stack = stack
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- 可用性 -----------------------------------------------------------

    def availability(self, *, identity) -> tuple[bool, str | None]:
        """这个会话能不能读整机读数。

        整机/整卡读数是**共享量**，不是个人数据，所以它有自己的 scope
        （``observability:host:read``），而不是「登录了就顺带能看」。

        判定读的是**由有效角色推出的观测词表**，不是 ``identity.requester.scopes``
        —— 后者是 Registry 那份词表。两套词表本来就该各管各的面：拿 Registry 的
        scope 去判观测的门，等于让一条只被授权读登记库的凭据看见整机读数。
        """
        granted = bff_scopes.observability_scopes_for(identity.effective_role)
        if observability_identity.SCOPE_HOST_READ in granted:
            return True, None
        return False, "missing_required_scope"

    # --- 判定 -------------------------------------------------------------

    def verdict(self, *, identity, now: datetime | None = None) -> HostSummaryVerdict:
        """取一次投影并**在服务端**判定它。失败一律封闭。

        三种结局，各自有各自的 HTTP 含义（由路由翻译）：

        * 会话没有 ``observability:host:read`` ⇒ ``AccessDenied`` ⇒ 403；
        * 这次没读到（``read()`` 返回 ``None``、抛错、或形状根本不是 mapping）
          ⇒ ``SourceUnavailable`` ⇒ 503；
        * 读到了 ⇒ 交给 ``validate_host_summary``，返回它的判定。**它接受不代表
          LIVE**：``accepted`` 说的是信封可信，每一个指标还要各自过一遍样本时间
          与单位。一份信封合法的响应里，指标仍可能全是 UNKNOWN。
        """
        available, reason = self.availability(identity=identity)
        if not available:
            raise observability_errors.AccessDenied(
                f"missing required scope {observability_identity.SCOPE_HOST_READ}"
                if reason == "missing_required_scope"
                else str(reason)
            )

        moment = now if now is not None else self._clock()
        try:
            payload = self.stack.read()
        except Exception as exc:  # noqa: BLE001 - 上游故障只能变成「没读到」
            raise observability_errors.SourceUnavailable(
                f"the host projection could not be read: {type(exc).__name__}"
            ) from None

        if payload is None:
            raise observability_errors.SourceUnavailable(
                "the host projection has not been read yet"
            )

        return validate_host_summary(
            payload,
            now=moment,
            max_sample_age_s=self.stack.max_sample_age_s,
            future_tolerance_s=self.stack.future_tolerance_s,
        )

    def summary(self, *, identity, now: datetime | None = None) -> dict:
        """判定结果 ⇒ 路由要下发的 ``data`` 块。原样透传判定器的输出。"""
        verdict = self.verdict(identity=identity, now=now)
        block = verdict.to_dict()
        block["schema_version"] = SCHEMA_VERSION
        block["route"] = PROJECTION_PATH
        return block

    # --- 能力清单要的那一个事实 -------------------------------------------

    def source_fact(self) -> dict:
        """这份来源「最后一次真的量到是什么时候」。

        采样型来源才需要这个方法，而它正是本清单里唯一声明了 TTL 的那一条
        （90 秒）。判定规则与观测口径一致：

        * 取**所有**带上 ``sample_at`` 的指标里最新的那一个 —— 包括被判过期的
          那些。用最新的那个算年龄，才能把 ``sample_aged_out`` 与
          ``no_sample_yet`` 分开：前者是「量到过、但太旧了」，后者是「从来没量到」。
        * ``observed_at`` 取信封自己的那个（已由判定器校验过时区与未来容差）。
        * 这次没读到 ⇒ 三个字段全是 ``None``，于是清单如实报 ``stale`` +
          ``no_sample_yet``，而不是把「读不到」翻译成 ``ready``。
        """
        try:
            payload = self.stack.read()
        except Exception:  # noqa: BLE001 - 能力清单不该因为上游抖一下就 500
            payload = None
        if payload is None:
            return {"sample_at": None, "observed_at": None, "ttl_seconds": None}

        verdict = validate_host_summary(
            payload,
            now=self._clock(),
            max_sample_age_s=self.stack.max_sample_age_s,
            future_tolerance_s=self.stack.future_tolerance_s,
        )
        newest: datetime | None = None
        for item in verdict.metrics.values():
            moment = parse_instant(item.sample_at)
            if moment is None:
                continue
            if newest is None or moment > newest:
                newest = moment
        observed = parse_instant(verdict.observed_at)
        return {
            "sample_at": newest,
            "observed_at": observed,
            "ttl_seconds": self.stack.max_sample_age_s,
        }


class DetachedHostSummaryAdapter:
    """没有装配主机投影：路由 503，而不是回一份全 UNKNOWN 的 200。

    全 UNKNOWN 的 200 与「本部署没有这条来源」形状几乎一样，但对运维是两个
    完全不同的结论：前者要去查投影端，后者要去查装配。让它们长得一样，
    是让一次接线遗漏伪装成一次数据故障。
    """

    name = "detached"
    attached = False
    reason = REASON_HOST_SUMMARY_NOT_ATTACHED


__all__ = [
    "REASON_HOST_SUMMARY_NOT_ATTACHED",
    "SCHEMA_VERSION",
    "DetachedHostSummaryAdapter",
    "HostSummaryStack",
    "ModuleHostSummaryAdapter",
]
