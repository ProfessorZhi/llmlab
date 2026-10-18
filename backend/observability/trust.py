"""受信来源契约：一个读数凭什么可以被当作「已验证」。

问题
----

在这之前，``VERIFIED_METRIC_SOURCES`` 这份白名单在仓库里有**三份副本**：

* ``backend/portal/modules.py``（模块视图的接线判定），
* ``backend/bff/observability_adapter.py``（指标卡片的降级判定），
* 以及前端 ``web/js/data/contract.js`` 里那份。

而 ``backend/observability`` —— **数据的产地** —— 一份都没有：``Provenance.source``
只校验「非空字符串」。于是「这个数字可不可信」这个判断，在最该有权威的地方缺席，
只能由两个消费方各自维护一份拷贝。任何一份漂移，都会得到一个「看起来在跑、
其实在撒谎」的面板。

本模块把那份白名单放回产地，并把它变成**一个可被查询的判定**，而不是一个散落的
集合字面量。

三条纪律
--------

1. **默认拒绝。** 白名单不是黑名单：``classify_source`` 对任何它不认识的名字返回
   ``unverified``。新增一个可信来源必须有人显式改这里 —— 也就是必须有一次
   「我声明这个东西现在可信」的评审。
2. **合成不等于不可信，但绝不可当 LIVE。** 合成来源（``mock`` /
   ``synthetic-fixture``）是**诚实**的数据，它有独立的分类，用于把「假数据」和
   「来路不明的数据」分开显示；但它**永远**不是 LIVE。
3. **名字必须逐字匹配。** ``prometheus-readonly`` 与 ``prometheus`` 是两个来源。
   这不是洁癖：``PrometheusProvider`` 曾经自称 ``"prometheus"``，而白名单里写的是
   ``"prometheus-readonly"`` —— 于是按契约接好线的读数会被消费方全部拒绝。
   这种「接对了却不显示」的失败方式比崩溃更难查，所以本模块用一条回归把它钉住。

本模块**不改变任何既有 DTO 的字段或校验**：它只是把「谁可信」这个判断集中到一处，
供消费方（BFF、Atlas 聚合、报告自检）与测试共用。
"""

from __future__ import annotations

__all__ = [
    "CLASS_SYNTHETIC",
    "CLASS_UNVERIFIED",
    "CLASS_VERIFIED",
    "SOURCE_CLASSES",
    "SOURCE_GATEWAY_USAGE_EXPORT",
    "SOURCE_MOCK",
    "SOURCE_NVML_VERIFIED",
    "SOURCE_PROMETHEUS_READONLY",
    "SYNTHETIC_SOURCES",
    "VERIFIED_SOURCES",
    "classify_source",
    "is_verified_source",
    "may_present_as_live",
]

#: Gateway 导出的调用元数据（服务端已按受信 key 目录归属）。
SOURCE_GATEWAY_USAGE_EXPORT = "gateway-usage-export"

#: 经确认可读的 NVML 观测（句柄由外部注入，模块自身不加载驱动）。
SOURCE_NVML_VERIFIED = "nvml-verified"

#: 只读 Prometheus 查询（固定表达式、环回端点）。注意 **不是** ``"prometheus"``。
SOURCE_PROMETHEUS_READONLY = "prometheus-readonly"

#: 离线合成来源。``MockMetricProvider`` 的自报名，``synthetic`` 恒为 True。
SOURCE_MOCK = "mock"

#: 唯一被信任的三个来源口径。与 ``backend/portal/modules.py``、
#: ``backend/bff/observability_adapter.py`` 和前端 ``contract.js`` 必须逐字一致；
#: ``tests/observability/test_source_trust.py`` 对这三处做奇偶校验。
VERIFIED_SOURCES = frozenset(
    {
        SOURCE_GATEWAY_USAGE_EXPORT,
        SOURCE_NVML_VERIFIED,
        SOURCE_PROMETHEUS_READONLY,
    }
)

#: 明确自报为合成的来源。它们**诚实**，但永远不是 LIVE。
SYNTHETIC_SOURCES = frozenset(
    {
        SOURCE_MOCK,
        "synthetic",
        "synthetic-fixture",
        "synthetic-demo",
        "demo",
    }
)

#: 来源分类的取值域。
CLASS_VERIFIED = "verified"
CLASS_SYNTHETIC = "synthetic"
CLASS_UNVERIFIED = "unverified"
SOURCE_CLASSES = (CLASS_VERIFIED, CLASS_SYNTHETIC, CLASS_UNVERIFIED)


def classify_source(source: object) -> str:
    """把一个来源名字归入 :data:`SOURCE_CLASSES` 之一。

    不认识的一律 ``unverified``（默认拒绝），而不是「大概可信」。
    ``None``、非字符串、空串同样是 ``unverified`` —— 「没有来源」不是一种来源。
    """
    if not isinstance(source, str):
        return CLASS_UNVERIFIED
    name = source.strip()
    if not name:
        return CLASS_UNVERIFIED
    if name in VERIFIED_SOURCES:
        return CLASS_VERIFIED
    if name in SYNTHETIC_SOURCES:
        return CLASS_SYNTHETIC
    return CLASS_UNVERIFIED


def is_verified_source(source: object) -> bool:
    """这个来源名字是否在白名单里。**不看** ``synthetic`` 标志。"""
    return classify_source(source) == CLASS_VERIFIED


def may_present_as_live(source: object, *, synthetic: bool) -> bool:
    """这份数据是否可以按 LIVE（经核验的真实读数）呈现。

    只有一条路径为真：**显式声明非合成** + **来源在受信白名单里**。

    刻意不看数据的「形状」、标题、单位或数量级 —— 那些都能被伪造，而来源声明
    不能（它来自受信配置，不来自请求）。``synthetic`` 用 ``is True`` 比较，
    所以 ``None`` / 缺省 / ``0`` 都不构成「非合成」的声明。
    """
    return synthetic is False and classify_source(source) == CLASS_VERIFIED
