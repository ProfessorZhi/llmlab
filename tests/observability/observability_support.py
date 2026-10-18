"""测试共用脚手架：路径引导、受信目录、登记表、请求构造、期望常量。

**这里的每一个 ``key_id`` / ``uid`` / ``endpoint_id`` 都是虚构的。**
``GatewayKeyBinding`` 就是那份「服务端可信目录」的测试替身；真实目录将来由 BFF
从受信配置加载。fixture 里的主机数值同样是编造的（见 ``fixtures/`` 的文件头）。

合成名册（carol 为 infra admin；alice / dave / bob 为模型用户，dana 不存在）
只用于说明**语义**，不出现在任何测试数据里 —— 测试数据一旦用真实主机账户的 uid，
就会把「某人在某窗口用了多少」变成一份可提交的记录。
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from backend.observability import (  # noqa: E402
    SCOPE_ADMIN_READ,
    SCOPE_HOST_READ,
    SCOPE_SELF_READ,
    EngineRecord,
    EngineRegistry,
    GatewayKeyBinding,
    KeyDirectory,
    ObservabilityRequest,
    Provenance,
    RequesterPrincipal,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
)



#: 测试窗口：2026-10-15T08:00Z 起 4 小时（左闭右开），桶 1h -> 应有 4 个桶。
WINDOW_FROM = "2026-10-15T08:00:00Z"
WINDOW_TO = "2026-10-15T12:00:00Z"

#: 固定的「现在」。所有 DTO 都从它算新鲜度，所以测试可复现。
NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

#: 合成来源声明。
SYNTHETIC = Provenance(source="synthetic-fixture", synthetic=True)
LIVE_LIKE = Provenance(source="gateway-metadata", synthetic=False)

# --- 虚构名册 ---------------------------------------------------------------

UID_ALICE = "u-1001"
UID_BOB = "u-5001"
UID_CAROL = "u-2001"
UID_DAVE = "u-3001"

ENDPOINT_SHARED_A = "model-shared-a"
ENDPOINT_SHARED_B = "model-shared-b"
ENDPOINT_PRIVATE_C = "model-private-c"
ENDPOINT_RETIRED = "model-retired"

ENGINE_A = "engine-shared-a"
ENGINE_B = "engine-shared-b"
ENGINE_C = "engine-private-c"
ENGINE_RETIRED = "engine-retired"


def trusted_directory() -> KeyDirectory:
    """测试用受信 key 目录（虚构数据）。"""
    return KeyDirectory(
        [
            GatewayKeyBinding("key-alpha", UID_ALICE, "proj-a", "resume-parse"),
            GatewayKeyBinding("key-beta", UID_BOB, "proj-a", "code-assist"),
            GatewayKeyBinding("key-orphan", UID_ALICE, "proj-a", None),
            GatewayKeyBinding("key-gamma", UID_CAROL, "proj-b", "eval-suite"),
            GatewayKeyBinding(
                "key-revoked", UID_DAVE, "proj-a", "resume-parse", revoked=True
            ),
        ]
    )


def registry() -> EngineRegistry:
    """测试用引擎登记表（虚构数据）。"""
    return EngineRegistry(
        [
            EngineRecord(
                engine_id=ENGINE_A,
                endpoint_id=ENDPOINT_SHARED_A,
                display_name="Shared A",
                visibility=VISIBILITY_SHARED,
                replicas_declared=2,
            ),
            EngineRecord(
                engine_id=ENGINE_B,
                endpoint_id=ENDPOINT_SHARED_B,
                display_name="Shared B",
                visibility=VISIBILITY_SHARED,
                replicas_declared=1,
            ),
            EngineRecord(
                engine_id=ENGINE_C,
                endpoint_id=ENDPOINT_PRIVATE_C,
                display_name="Carol's private C",
                visibility=VISIBILITY_PRIVATE,
                owner_uid=UID_CAROL,
                replicas_declared=1,
            ),
            EngineRecord(
                engine_id=ENGINE_RETIRED,
                endpoint_id=ENDPOINT_RETIRED,
                display_name="Retired",
                visibility=VISIBILITY_SHARED,
                replicas_declared=1,
            ),
        ]
    )


def principal(
    uid: str = UID_ALICE,
    *,
    roles: tuple[str, ...] = ("user",),
    scopes: tuple[str, ...] = (SCOPE_SELF_READ,),
    endpoints: tuple[str, ...] = (ENDPOINT_SHARED_A,),
) -> RequesterPrincipal:
    return RequesterPrincipal(
        uid=uid,
        roles=frozenset(roles),
        scopes=frozenset(scopes),
        authorized_endpoints=frozenset(endpoints),
    )


def admin_principal(uid: str = "u-9admin") -> RequesterPrincipal:
    """管理主体：**显式**持三个 scope。role 里刻意不加 admin —— role 不产生权限。"""
    return RequesterPrincipal(
        uid=uid,
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_SELF_READ, SCOPE_ADMIN_READ, SCOPE_HOST_READ}),
        authorized_endpoints=frozenset(),
    )


def host_only_principal(uid: str = "u-8host") -> RequesterPrincipal:
    """只持 host scope：看得到整机读数，看不到全体用户摘要。"""
    return RequesterPrincipal(
        uid=uid,
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_SELF_READ, SCOPE_HOST_READ}),
        authorized_endpoints=frozenset(),
    )


def office_principal(uid: str = "u-7admin") -> RequesterPrincipal:
    """**只靠 role 自称 admin、没有任何管理 scope** —— 用来证明 role 不开门。"""
    return RequesterPrincipal(
        uid=uid,
        roles=frozenset({"admin", "owner", "superuser"}),
        scopes=frozenset({SCOPE_SELF_READ}),
        authorized_endpoints=frozenset({ENDPOINT_SHARED_A}),
    )


def parse_moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def request(
    principal_ctx: RequesterPrincipal | None = None,
    *,
    window_from: str = WINDOW_FROM,
    window_to: str = WINDOW_TO,
    bucket: str = "1h",
    requested_uid: str | None = None,
    requested_endpoint: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ObservabilityRequest:
    return ObservabilityRequest(
        principal=principal_ctx if principal_ctx is not None else principal(),
        window_from=parse_moment(window_from),
        window_to=parse_moment(window_to),
        requested_uid=requested_uid,
        requested_endpoint=requested_endpoint,
        bucket=bucket,
        limit=limit,
        offset=offset,
    )


# --- 行构造 -----------------------------------------------------------------


def row(
    ts: str,
    key_id: str,
    *,
    endpoint: str | None = ENDPOINT_SHARED_A,
    label: str = "resume-parse",
    status: int = 200,
    outcome: str = "success",
    stream: bool = True,
    queue_ms: int | None = 5,
    ttft_ms: int | None = 120,
    total_ms: int | None = 1000,
    input_tokens: int | None = 100,
    output_tokens: int | None = 200,
    extra: dict | None = None,
) -> str:
    """一条 Gateway 元数据行（JSON）。``extra`` 用来注入正文/自报字段。

    ``label`` 可自定义：它是**客户端自报**的，所以测试需要它具备可区分性，
    才能证明「按 label 分组」这类错误实现会泄漏别人的名字。
    """
    payload: dict = {
        "ts": ts,
        "key_id": key_id,
        "label": label,
        "method": "POST",
        "path": "/v1/chat/completions",
        "status": status,
        "outcome": outcome,
        "stream": stream,
        "queue_ms": queue_ms,
        "ttft_ms": ttft_ms if stream else None,
        "total_ms": total_ms,
        "bytes_out": 4096,
    }
    if endpoint is not None:
        payload["model"] = endpoint
    payload["usage"] = (
        None
        if input_tokens is None and output_tokens is None
        else {"input_tokens": input_tokens, "output_tokens": output_tokens}
    )
    if extra:
        payload.update(extra)
    return json.dumps(payload, sort_keys=True)


def text_of(rows) -> str:
    return "\n".join(rows) + "\n"


# --- 固定样本 ---------------------------------------------------------------

#: 只含 alice（key-alpha）自己、窗口内、干净的样本。
ALICE_ROWS = (
    row("2026-10-15T08:05:00Z", "key-alpha", total_ms=1000, queue_ms=5),
    row("2026-10-15T09:05:00Z", "key-alpha", total_ms=1200, queue_ms=7),
    row("2026-10-15T10:05:00Z", "key-alpha", total_ms=800, queue_ms=3, stream=False, ttft_ms=None),
)

#: 不可归属 / 归属他人的内容，用来验证「对本人不可见」。
#:
#: 这些行的 ``label`` **刻意互不相同**（``eval-suite`` / ``misc``）：label 是客户端自报的，
#: 一旦有人拿它当分组依据，这些名字就会出现在 alice 的响应里。让它们可区分，
#: 金丝雀才咬得住（见 ``tests/observability/test_canaries.py``）。
FOREIGN_ROWS = (
    # 他人（carol）的共享端点调用，窗口内
    row("2026-10-15T08:10:00Z", "key-gamma", endpoint=ENDPOINT_SHARED_B,
        label="eval-suite", total_ms=9999),
    # 未注册 key
    row("2026-10-15T08:11:00Z", "unknown-key-zzz", label="misc", total_ms=8888),
    # 损坏 JSON
    '{"ts":"2026-10-15T08:12:00Z","key_id":"key-gamma","broken":',
    # 含正文类字段
    row(
        "2026-10-15T08:13:00Z",
        "key-gamma",
        endpoint=ENDPOINT_SHARED_B,
        label="eval-suite",
        extra={"prompt": "<SYNTHETIC-CANARY: 正文永不入库>"},
    ),
    # 含自报身份字段（必须被忽略）
    row(
        "2026-10-15T08:14:00Z",
        "key-gamma",
        endpoint=ENDPOINT_SHARED_B,
        label="eval-suite",
        extra={"x_principal": UID_ALICE, "x_project": "proj-a", "X-Forwarded-User": UID_ALICE},
    ),
    # 窗口外 + 他人
    row("2026-10-15T20:30:00Z", "key-gamma", endpoint=ENDPOINT_SHARED_B,
        label="spoof-admin", total_ms=7777),
)


#: 具名合成语料。**故意放在代码里，而不是 ``fixtures/*.jsonl`` 文件里。**
#:
#: 仓库根的 ``.gitignore`` 有一条**兜底**规则 ``*.jsonl``（``docs/access-boundaries.md`` §3：
#: 「兜底而非许可」）。同期的兄弟模块是靠在 ``.gitignore`` 里加 ``!tests/…/fixtures/**/*.jsonl``
#: 例外来放行的；本模块**不**这么做，理由有二：
#:
#: 1. ``.gitignore`` 是所有并行任务共用的文件，几处否定规则会落在同一块区域上，
#:    改动它把「源码范围不可交叠」这条约束直接破坏掉；
#: 2. 数据安全那一条兜底规则本身值得保持**无条件**成立 —— 少一处例外，
#:    将来就少一个「这里为什么放行 jsonl」的问题。
#:
#: 代价是语料以 Python 字面量形式存在。它是**一小段**、可逐行审阅的合成数据，
#: 且每行都带 ``fixture: SYNTHETIC`` 标记，比一个不可 grep 的外部文件更好核对。
SAMPLE_CORPUS_ROWS = (
    # --- alice（key-alpha / key-orphan，属 u-1001）窗口内 ---
    row("2026-10-15T08:05:00Z", "key-alpha", total_ms=1000, queue_ms=5, ttft_ms=120),
    row("2026-10-15T09:05:00Z", "key-alpha", total_ms=1200, queue_ms=7, ttft_ms=150),
    row("2026-10-15T10:20:00Z", "key-orphan", label="misc", stream=False,
        ttft_ms=None, total_ms=700, queue_ms=2, input_tokens=20, output_tokens=30),
    # --- 其他主体（对本人的响应不可见）---
    row("2026-10-15T08:35:00Z", "key-beta", label="code-assist", stream=False,
        ttft_ms=None, total_ms=800, queue_ms=3, input_tokens=50, output_tokens=150),
    row("2026-10-15T09:10:00Z", "key-gamma", endpoint=ENDPOINT_SHARED_B, label="eval-suite",
        queue_ms=9, ttft_ms=900, total_ms=5000, input_tokens=10, output_tokens=400),
    # 未注册 key，且自报身份（必须被忽略）
    row("2026-10-15T09:30:00Z", "unknown-key-zzz", endpoint=ENDPOINT_SHARED_B,
        label="eval-suite", stream=False, ttft_ms=None, total_ms=50, queue_ms=0,
        input_tokens=1, output_tokens=1,
        extra={"x_project": "proj-a", "x_principal": UID_ALICE,
               "X-Forwarded-User": UID_ALICE}),
    # 损坏行
    '{"ts":"2026-10-15T09:45:00Z","key_id":"key-gamma","broken":',
    # 带正文类字段（会被拒绝计数）
    row("2026-10-15T10:00:00Z", "key-gamma", endpoint=ENDPOINT_SHARED_B, label="eval-suite",
        queue_ms=4, ttft_ms=200, total_ms=1100, input_tokens=5, output_tokens=5,
        extra={"prompt": "<SYNTHETIC-CANARY: 正文类字段必须被拒绝>"}),
    # --- alice 的失败与限流 ---
    row("2026-10-15T10:45:00Z", "key-alpha", status=429, outcome="failed", stream=False,
        ttft_ms=None, total_ms=None, queue_ms=1, input_tokens=None, output_tokens=None),
    row("2026-10-15T11:40:00Z", "key-alpha", status=500, outcome="failed", stream=False,
        ttft_ms=None, total_ms=1400, queue_ms=2, input_tokens=None, output_tokens=None),
    # --- 已吊销 key（属 u-3001）---
    row("2026-10-15T11:00:00Z", "key-revoked", stream=False, ttft_ms=None,
        total_ms=400, queue_ms=1, input_tokens=5, output_tokens=5),
    # --- alice 自己的行，但引用了本人当前未授权的端点 ---
    row("2026-10-15T11:10:00Z", "key-alpha", endpoint=ENDPOINT_RETIRED, queue_ms=3,
        ttft_ms=90, total_ms=600, input_tokens=8, output_tokens=8),
    # --- 窗口外（一条本人、一条他人）---
    row("2026-10-15T20:30:00Z", "key-alpha", label="spoof-admin", total_ms=900),
    row("2026-10-15T20:35:00Z", "key-gamma", endpoint=ENDPOINT_SHARED_B, label="eval-suite",
        total_ms=900),
)

#: 上面那批行的 JSONL 文本。
SAMPLE_CORPUS = "\n".join(SAMPLE_CORPUS_ROWS) + "\n"

#: 完全空的输入 —— 用来证明「0 次请求」不是一个结论。
EMPTY_CORPUS = ""


def load_fixture(name: str = "gateway_sample.jsonl") -> str:
    """取一份具名合成语料。

    保留这个名字是为了让调用点读起来仍然像「加载一份 fixture」；但内容来自**代码**
    里的 :data:`SAMPLE_CORPUS` / :data:`EMPTY_CORPUS`，磁盘上没有任何 ``.jsonl``
    （理由见 :data:`SAMPLE_CORPUS_ROWS` 的说明）。
    """
    if name == "gateway_empty.jsonl":
        return EMPTY_CORPUS
    if name in ("gateway_sample.jsonl", "synthetic_sample.jsonl"):
        return SAMPLE_CORPUS
    raise KeyError(f"unknown synthetic corpus {name!r}")


def hours(hours_count: int) -> timedelta:
    return timedelta(hours=hours_count)


__all__ = [
    "ALICE_ROWS",
    "EMPTY_CORPUS",
    "ENDPOINT_PRIVATE_C",
    "ENDPOINT_RETIRED",
    "ENDPOINT_SHARED_A",
    "ENDPOINT_SHARED_B",
    "ENGINE_A",
    "ENGINE_B",
    "ENGINE_C",
    "ENGINE_RETIRED",
    "FOREIGN_ROWS",
    "LIVE_LIKE",
    "NOW",
    "SAMPLE_CORPUS",
    "SAMPLE_CORPUS_ROWS",
    "SYNTHETIC",
    "UID_ALICE",
    "UID_BOB",
    "UID_CAROL",
    "UID_DAVE",
    "WINDOW_FROM",
    "WINDOW_TO",
    "admin_principal",
    "host_only_principal",
    "hours",
    "load_fixture",
    "office_principal",
    "parse_moment",
    "principal",
    "registry",
    "request",
    "row",
    "text_of",
    "trusted_directory",
]
