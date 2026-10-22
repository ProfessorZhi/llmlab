"""来源能力清单：每个来源**此刻对这个会话**能说什么。

Issue #79 要的是一个前端可以据以分支的稳定判据。在它出现之前，前端只能从
「那条路由回了我什么」反推「这个来源到底接没接上」——而那是一个**无法区分**
下面两种情形的推理：

* 服务端**没有**这个来源（``not_configured``）；
* 服务端有这个来源，但**这条会话**没有读它的权限（``auth_required``）。

两者在前端都会表现成「没有数据」。把「这里本来就没有这东西」和「你没有被授权」
画成同一块空白，是运维事故最常见的开端。所以本模块把它们拆成四个**封闭取值**：

``ready``
    适配器已装配 **且** 这条会话持有它要求的 scope **且** 最近一个真实样本仍在
    声明的 TTL 之内。
``stale``
    适配器已装配、权限也有，但**没有落在 TTL 内的样本**。它同时覆盖「从来没有
    采到过样本」（``no_sample_yet``）与「采到过但已经过期」（``sample_aged_out``）
    —— 状态的**取值**是封闭的四个，区分靠 ``reason``。这两件事对前端是同一个
    决定（不要拿它当当前读数），对运维是两个不同的排查方向。
``auth_required``
    适配器在，但这条会话缺它要求的 scope。这是一个**会话**事实，不是部署事实。
``not_configured``
    适配器没装配。这是**部署**事实，换个人登录也不会变。

几个刻意的设计
--------------

**``ready`` 需要正面证据，而不是「没有反证」。** 一个来源只有在适配器真的被
装配、scope 真的被授予、样本真的落在 TTL 之内时才可能是 ``ready``。缺任何一项
都落到另外三个取值之一。把「没人说它坏了」当成 ``ready``，正是本仓库一路拒绝的
那种推断。

**过不过期只按 ``sample_at`` 判，不按 ``observed_at``。** 契约把这两个时间分得
很清楚：``sample_at`` 是**生产者真的量到那一下**，``observed_at`` 是**服务端看到
它的那一下**。一个刚刚从上游取回来的、三天前的样本，``observed_at`` 很新而
``sample_at`` 很旧 —— 拿后者当新鲜度，会把一条三天前的曲线画成实时。所以
TTL 判定只读 ``sample_at``，``observed_at`` 只是原样报给前端。

**未来时间戳不是「很新」。** 与 ``backend.observability.host_projection`` 同一个
口径：``sample_at`` 领先「现在」超过容差就是时钟偏斜，状态是 ``stale`` 而不是
``ready``。两份判定用同一个稳定 reason，前端只需要认识一次。

**``ttl_seconds=None`` 不是「不过期」，是「新鲜度不由 TTL 管」。** 有些来源是
**一次实时查询**：权威在这一次请求里被读到，「读到了」本身就是新鲜度证据。
Register、Atlas 的调用语料、观测模块都属这一类 —— 它们各自 DTO 里已有的
per-metric / per-corpus 口径继续由它们自己负责，本清单不替它们编一根阈值。
反过来，**声明了阈值**的两条（主机投影 90 秒、评测报告目录 3600 秒）恰恰是
上游契约里真的写了数的两条，那两个数字从各自的模块 import，不在这里重抄。

**这份清单同时就是「DTO seam 声明」。** :data:`SOURCE_SEAMS` 逐条写下一个来源
由哪条 Lane 提供、走哪条同源路由、要求哪个 scope、新鲜度阈值多少。其它 Lane
要接进来，接的是这张表里的一个 :class:`SourceSeam`，不是一份口口相传的约定。
把 seam 写进代码而不是只写进文档，是因为文档里的那一份不会被测试读。

本模块**不做 IO、不联网、不 import 任何 provider 实现**：它接的是调用方已经
装配好的「证据」，因此可以在没有 Dell、没有 PAM、没有 Gateway 的环境里逐条验收。
（scope 与 TTL 那几行 import 的是各模块**自己声明的常量**，只读，且正是为了
不产生第二份会漂移的写法。）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..evaluation.identity import SCOPE_EVAL_READ as eval_read_scope
from ..observability.dto import iso_z
from ..observability.host_projection import (
    DEFAULT_FUTURE_TOLERANCE_S as host_future_tolerance_s,
)
from ..observability.host_projection import (
    DEFAULT_MAX_SAMPLE_AGE_S as host_max_sample_age_s,
)
from ..observability.identity import SCOPE_HOST_READ as obs_host_scope
from ..observability.identity import SCOPE_SELF_READ as obs_self_scope

SCHEMA_VERSION = "portal-capabilities/1"

# --- 状态：封闭取值 ---------------------------------------------------------

STATE_READY = "ready"
STATE_STALE = "stale"
STATE_AUTH_REQUIRED = "auth_required"
STATE_NOT_CONFIGURED = "not_configured"

#: 前端的可用性分支只看这四个。新增一个取值必须同时改
#: ``tests/bff/test_capabilities.py`` 里那条封闭集合断言 —— 那正是要被逼着做的
#: 一次思考：这个新状态在前端要画成什么？
STATES = (STATE_READY, STATE_STALE, STATE_AUTH_REQUIRED, STATE_NOT_CONFIGURED)

# --- 原因：稳定 token -------------------------------------------------------

#: 部署事实：这个来源没有被装配。
REASON_ADAPTER_NOT_ATTACHED = "adapter_not_attached"
#: 会话事实：适配器在，这条会话没有读它的 scope。
REASON_MISSING_SCOPE = "missing_required_scope"
#: 装配了、也授权了，但从来没有采到过样本。
REASON_NO_SAMPLE_YET = "no_sample_yet"
#: 采到过样本，但它已经超出声明的 TTL。
REASON_SAMPLE_AGED_OUT = "sample_aged_out"
#: 样本时间在将来，超出容差 —— 时钟偏斜，不是「很新」。
REASON_SAMPLE_IN_FUTURE = "sample_in_future"

REASONS = (
    REASON_ADAPTER_NOT_ATTACHED,
    REASON_MISSING_SCOPE,
    REASON_NO_SAMPLE_YET,
    REASON_SAMPLE_AGED_OUT,
    REASON_SAMPLE_IN_FUTURE,
)

#: 允许 ``sample_at`` 领先「现在」的容差（秒）。**直接取主机投影那一个常量**：
#: 「样本时间在将来」是一个口径，本清单不另立一个数。
DEFAULT_FUTURE_TOLERANCE_S = host_future_tolerance_s

# --- Lane 归属 --------------------------------------------------------------

#: V1 Batch-1 的 Lane 编号（见 Issue #80 的分派表）。与 ``route_map`` 里那套
#: ``IAM#45 / Data#47 / Integration#49`` 是同一批团队在不同规划轮次下的编号，
#: 不是两套组织。这里用 V1 的编号，因为本清单是 V1 契约的一部分，而
#: ``route_map`` 的 ``owner`` 字段继续用它自己那套。
OWNER_HOST = "Host#74"
OWNER_IAM = "IAM#75"
OWNER_REGISTRY = "Registry#76"
OWNER_CALLS = "Calls#77"
OWNER_EVALUATION = "Evaluation#78"
OWNER_BFF = "BFF#79"

# --- 装配形态 ---------------------------------------------------------------

#: 这个来源由一个**可选注入的适配器**提供，适配器自己报 ``attached``。
ATTACHMENT_ADAPTER = "adapter"
#: 这个来源是应用的**必需依赖**：构造不出它，应用根本不存在，也就没有这条路由。
#: 「在」就是「装了」，没有一个「装了但关着」的中间态。
ATTACHMENT_REQUIRED = "required"

ATTACHMENTS = (ATTACHMENT_ADAPTER, ATTACHMENT_REQUIRED)


@dataclass(frozen=True)
class SourceSeam:
    """一个来源的**装配接缝**：它是谁、喂哪条路由、要什么权限、多新算新。

    ``adapter_attribute`` 是 ``OpsApplication`` 上那个可注入的适配器属性名。
    写下来是为了让「清单里宣称的这个来源」与「应用里真的装配的那个对象」可以被
    同一条测试对上 —— 一份只在文档里成立的 seam，就是一条会漂移的规则。

    ``attachment`` 说明那个属性应当怎么读：一个可选适配器（自己报 ``attached``），
    还是一个必需依赖。把它写成封闭取值而不是「按名字特判」，是因为
    「为什么 Registry 永远是已装配」应该是一条能被断言、能被读到的声明，
    而不是 app.py 里一个 ``if seam.source == "registry"``。
    """

    source: str
    owner: str
    route: str
    scope: str | None
    ttl_seconds: int | None
    adapter_attribute: str
    attachment: str = ATTACHMENT_ADAPTER
    note: str = ""

    def __post_init__(self) -> None:
        if self.attachment not in ATTACHMENTS:
            raise ValueError(f"unknown attachment kind: {self.attachment!r}")

    def __post_init__(self) -> None:
        if not self.source or "/" in self.source:
            raise ValueError("a source id is a bare token, not a path")
        if not self.route.startswith("/"):
            raise ValueError("route must be an absolute same-origin path")
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive when declared")


#: 声明式 seam 表。**这是这份清单唯一的真源**：状态判定读它，seam 交接也读它。
SOURCE_SEAMS: tuple[SourceSeam, ...] = (
    SourceSeam(
        source="registry",
        owner=OWNER_REGISTRY,
        route="/api/v2/ops",
        # 行级 ACL 在 Registry 的 AccessPolicy 里判定，本层不设 scope 门槛：
        # 任何已登录成员都能读**自己**可见的那一份。写 ``None`` 是声明这件事，
        # 不是「忘了填」。
        scope=None,
        # 登记库是一次实时查询，不是采样源：见模块说明里 ``ttl_seconds=None`` 的
        # 那一段。给它编一根阈值，就是替 Registry 宣称一个它没说过的新鲜度口径。
        ttl_seconds=None,
        adapter_attribute="service",
        # Registry 的权威是应用**必需**的那个 service 对象：没有它就没有
        # ``/api/v2/ops`` 这条路由。它没有「装了但关着」的中间态。
        attachment=ATTACHMENT_REQUIRED,
        note="可见性由 Registry 判定；这是登记库查询，不由 TTL 管新鲜度。",
    ),
    SourceSeam(
        source="observability",
        owner=OWNER_HOST,
        route="/api/v2/ops/usage",
        scope=obs_self_scope,
        # 观测模块的新鲜度是**per-metric** 的（``MetricSpec.stale_after_seconds``：
        # 多数 60 秒，service/engine 120 秒），没有单一的部署级阈值可以直接搬。
        # 搬一个来会与它自己的 DTO 打架，所以这里如实写「不由 TTL 管」。
        ttl_seconds=None,
        adapter_attribute="observability",
        note="整机读数还要 observability:host:read，那是另一个 scope。",
    ),
    SourceSeam(
        source="host_summary",
        owner=OWNER_HOST,
        route="/api/infra/summary",
        scope=obs_host_scope,
        # 90 秒取自 host_projection 自己的 DEFAULT_MAX_SAMPLE_AGE_S，不重抄。
        ttl_seconds=host_max_sample_age_s,
        adapter_attribute="host_summary",
        note="固定 Prometheus/NVML 只读投影，服务端严格校验来源与样本时间。",
    ),
    SourceSeam(
        source="atlas_calls",
        owner=OWNER_CALLS,
        route="/api/atlas/calls",
        scope="atlas:calls:self:read",
        ttl_seconds=None,
        adapter_attribute="atlas",
        note="只报**本人**的调用元数据；这是对语料的一次实时查询。",
    ),
    SourceSeam(
        source="evaluation",
        owner=OWNER_EVALUATION,
        route="/api/evaluation/reports",
        scope=eval_read_scope,
        # 报告目录的新鲜度窗口（评测模块自己的 ``DEFAULT_STALENESS_SECONDS``，3600 秒）
        # 是**逐来源**判的，且已经在评测模块内部判定并写进它自己的 DTO。在本层再
        # 判一次，会得到「清单说 ready、DTO 说 stale」这种自相矛盾的两份答案。
        ttl_seconds=None,
        adapter_attribute="evaluation",
        note="报告目录的可见性与来源隔离由评测模块判定，本层只做桥接。",
    ),
)

SOURCES = tuple(seam.source for seam in SOURCE_SEAMS)
SEAMS_BY_SOURCE: dict[str, SourceSeam] = {seam.source: seam for seam in SOURCE_SEAMS}


@dataclass(frozen=True)
class SourceEvidence:
    """调用方交给本模块的**已装配事实**。

    这是适配器与清单之间唯一的接口：清单不 import 任何 provider，适配器也不
    认识清单。``attached`` 是**装配**事实，``sample_at`` 是**测量**事实，两者
    分开是因为它们由不同的人负责 —— 前者是运维的接线，后者是上游的数据。
    """

    attached: bool
    sample_at: datetime | None = None
    observed_at: datetime | None = None
    #: 本来源自己的新鲜度阈值。``None`` 表示用 seam 上声明的那个。
    ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        for name in ("sample_at", "observed_at"):
            moment = getattr(self, name)
            if moment is None:
                continue
            if not isinstance(moment, datetime) or moment.utcoffset() is None:
                raise ValueError(f"{name} must be a timezone-aware datetime")


@dataclass(frozen=True)
class SourceCapability:
    """一个来源此刻的判定结果。

    构造即校验：``ready`` 不能带 ``reason``，非 ``ready`` 必须带 —— 这条不变式
    让「一个说不出为什么不可用的来源」在构造时就被拒绝，而不是等到前端把它画成
    一块没有理由的空白。
    """

    source: str
    owner: str
    route: str
    scope: str | None
    state: str
    reason: str | None
    attached: bool
    sample_at: str | None
    observed_at: str | None
    age_seconds: float | None
    ttl_seconds: int | None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown capability state: {self.state!r}")
        if self.state == STATE_READY:
            if self.reason is not None:
                raise ValueError("a ready source must not carry a reason")
            if not self.attached:
                raise ValueError("a ready source must be attached")
        else:
            if self.reason is None:
                raise ValueError("a non-ready source must say why")
            if self.reason not in REASONS:
                raise ValueError(f"unknown capability reason: {self.reason!r}")
        if self.reason == REASON_ADAPTER_NOT_ATTACHED and self.attached:
            raise ValueError("adapter_not_attached contradicts attached=True")
        if self.state == STATE_NOT_CONFIGURED and self.attached:
            raise ValueError("not_configured is a deployment fact, so nothing is attached")

    @property
    def is_ready(self) -> bool:
        return self.state == STATE_READY

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "owner": self.owner,
            "route": self.route,
            "scope": self.scope,
            "state": self.state,
            "reason": self.reason,
            "attached": self.attached,
            "sample_at": self.sample_at,
            "observed_at": self.observed_at,
            "age_seconds": self.age_seconds,
            "ttl_seconds": self.ttl_seconds,
        }


def judge(
    seam: SourceSeam,
    evidence: SourceEvidence,
    *,
    granted_scopes: frozenset[str],
    now: datetime,
    future_tolerance_s: int = DEFAULT_FUTURE_TOLERANCE_S,
) -> SourceCapability:
    """一条 seam + 一份证据 + 这个会话的 scope ⇒ 一个判定。纯函数。

    判定顺序本身就是优先级，而且顺序是刻意的：**先**问「部署里有没有它」，
    **再**问「这条会话能不能读它」，最后才问「它新不新」。反过来的话，一个没有
    权限的会话会拿到「数据过期了」这种只有有权限的人才该知道的信息。
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)

    ttl = evidence.ttl_seconds if evidence.ttl_seconds is not None else seam.ttl_seconds

    if not evidence.attached:
        return _verdict(
            seam,
            evidence,
            state=STATE_NOT_CONFIGURED,
            reason=REASON_ADAPTER_NOT_ATTACHED,
            ttl=ttl,
            sample_at=None,
            age=None,
        )

    if seam.scope is not None and seam.scope not in granted_scopes:
        return _verdict(
            seam,
            evidence,
            state=STATE_AUTH_REQUIRED,
            reason=REASON_MISSING_SCOPE,
            ttl=ttl,
            sample_at=None,
            age=None,
        )

    if evidence.sample_at is None:
        return _verdict(
            seam,
            evidence,
            state=STATE_STALE,
            reason=REASON_NO_SAMPLE_YET,
            ttl=ttl,
            sample_at=None,
            age=None,
        )

    sample_at = evidence.sample_at.astimezone(timezone.utc)
    age = (now - sample_at).total_seconds()
    if age < -future_tolerance_s:
        return _verdict(
            seam,
            evidence,
            state=STATE_STALE,
            reason=REASON_SAMPLE_IN_FUTURE,
            ttl=ttl,
            sample_at=sample_at,
            age=age,
        )
    if ttl is not None and age > ttl:
        return _verdict(
            seam,
            evidence,
            state=STATE_STALE,
            reason=REASON_SAMPLE_AGED_OUT,
            ttl=ttl,
            sample_at=sample_at,
            age=age,
        )

    return _verdict(
        seam,
        evidence,
        state=STATE_READY,
        reason=None,
        ttl=ttl,
        sample_at=sample_at,
        age=max(0.0, age),
    )


def _verdict(
    seam: SourceSeam,
    evidence: SourceEvidence,
    *,
    state: str,
    reason: str | None,
    ttl: int | None,
    sample_at: datetime | None,
    age: float | None,
) -> SourceCapability:
    return SourceCapability(
        source=seam.source,
        owner=seam.owner,
        route=seam.route,
        scope=seam.scope,
        state=state,
        reason=reason,
        attached=bool(evidence.attached),
        sample_at=iso_z(sample_at) if sample_at is not None else None,
        observed_at=(
            iso_z(evidence.observed_at.astimezone(timezone.utc))
            if evidence.observed_at is not None
            else None
        ),
        age_seconds=age,
        ttl_seconds=ttl,
    )


def evidence_for(seam: SourceSeam, adapter: object, *, now: datetime) -> SourceEvidence:
    """一条 seam + 应用上那个属性 ⇒ 一份证据。按 seam 声明的**装配形态**分流。

    ``ATTACHMENT_REQUIRED`` 的来源，缺了它应用根本构造不出来；能走到这里就说明
    它在。所以「不是 ``None``」等于「装了」—— 而 ``None`` 仍然如实报「没装配」，
    因为一个构造期本应拦住它的人今天没拦住，不该被这里悄悄补上。
    """
    if seam.attachment == ATTACHMENT_REQUIRED:
        if adapter is None:
            return SourceEvidence(attached=False)
        return SourceEvidence(attached=True, sample_at=now, observed_at=now)
    return evidence_from(adapter, now=now)


def evidence_from(adapter: object, *, now: datetime) -> SourceEvidence:
    """一个已注入的适配器对象 ⇒ 一份证据。**这是适配器与清单之间唯一的翻译。**

    三种结局，各自诚实：

    * 适配器缺席（``None``），或它自称 ``attached = False`` ⇒ **没装配**。
      它没被问一个它回答不了的问题。
    * 适配器提供 ``source_fact()`` ⇒ 用它报出来的**真实**时间。采样型来源
      （主机投影）走这条：它们真的有一个「最后一次量到是什么时候」。
    * 其余 ⇒ ``sample_at = now``：这类来源是**一次实时查询**，权威在这次请求里
      被读到，「读到了」本身就是新鲜度证据。它们**没有** ``source_fact()``，
      不是忘了实现——给它们编一个采样时间，正是本清单要避免的那种假证据。

    ``source_fact()`` 返回一个可选键的映射（``sample_at`` / ``observed_at`` /
    ``ttl_seconds``）。它只报事实，不做判定——判定在 :func:`judge` 里，两件事
    分开，是为了让「谁说的」和「怎么判的」都能被单独测。
    """
    if adapter is None:
        return SourceEvidence(attached=False)
    if not bool(getattr(adapter, "attached", False)):
        return SourceEvidence(attached=False)

    probe = getattr(adapter, "source_fact", None)
    if not callable(probe):
        return SourceEvidence(attached=True, sample_at=now, observed_at=now)

    fact = probe() or {}
    if not isinstance(fact, dict):
        raise ValueError("source_fact() must return a mapping")
    return SourceEvidence(
        attached=True,
        sample_at=fact.get("sample_at"),
        observed_at=fact.get("observed_at"),
        ttl_seconds=fact.get("ttl_seconds"),
    )


def build_capabilities(
    *,
    subject: str,
    granted_scopes: frozenset[str],
    evidence: dict[str, SourceEvidence],
    now: datetime,
    future_tolerance_s: int = DEFAULT_FUTURE_TOLERANCE_S,
) -> dict:
    """组装一份 ``portal-capabilities/1``。纯函数：只读，不写任何东西。

    ``evidence`` 里**没有**的 seam 一律按「没装配」判 —— 少给一个来源的证据，
    结局是那个来源说自己 ``not_configured``，而不是它默默消失。一份清单里少一行
    和一个来源没接上，对前端是两件不同的事。
    """
    capabilities = [
        judge(
            seam,
            evidence.get(seam.source, SourceEvidence(attached=False)),
            granted_scopes=granted_scopes,
            now=now,
            future_tolerance_s=future_tolerance_s,
        )
        for seam in SOURCE_SEAMS
    ]
    counted = {state: 0 for state in STATES}
    for item in capabilities:
        counted[item.state] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": iso_z(now.astimezone(timezone.utc)),
        "scope": {
            "subject": subject,
            "scopes": sorted(granted_scopes),
        },
        "sources": [item.to_dict() for item in capabilities],
        "summary": counted,
    }


__all__ = [
    "ATTACHMENTS",
    "ATTACHMENT_ADAPTER",
    "ATTACHMENT_REQUIRED",
    "DEFAULT_FUTURE_TOLERANCE_S",
    "OWNER_BFF",
    "OWNER_CALLS",
    "OWNER_EVALUATION",
    "OWNER_HOST",
    "OWNER_IAM",
    "OWNER_REGISTRY",
    "REASONS",
    "REASON_ADAPTER_NOT_ATTACHED",
    "REASON_MISSING_SCOPE",
    "REASON_NO_SAMPLE_YET",
    "REASON_SAMPLE_AGED_OUT",
    "REASON_SAMPLE_IN_FUTURE",
    "SCHEMA_VERSION",
    "SEAMS_BY_SOURCE",
    "SOURCES",
    "SOURCE_SEAMS",
    "STATES",
    "STATE_AUTH_REQUIRED",
    "STATE_NOT_CONFIGURED",
    "STATE_READY",
    "STATE_STALE",
    "SourceCapability",
    "SourceEvidence",
    "SourceSeam",
    "build_capabilities",
    "evidence_for",
    "evidence_from",
    "judge",
]
