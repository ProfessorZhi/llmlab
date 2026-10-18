"""Atlas Gateway 数据适配与安全聚合。

不创建 HTTP listener，不接生产 Gateway，不读取真实 usage.jsonl。

``errors`` / ``identity`` / ``ingest`` / ``stats`` / ``service`` / ``aggregate`` /
``dto`` 是**纯函数、无 IO** 的一层，本文件导出的正是这一层。

``corpus`` 是**接入 seam**（受信目录文档加载 + Gateway 语料有界读取），它是本包
唯一会读本地文件的地方，因此**刻意不被本文件重新导出**：纯函数面与「会碰文件系统
的那一块」分得开，import 本包的人就不会随手把一条 IO 路径拖进来。需要它请显式
``from backend.atlas import corpus``。契约见 docs/atlas-data-contract.md §12。
"""

from .aggregate import (
    MAX_MODELS,
    MODEL_LABEL_ESCAPE,
    MODEL_OTHER,
    MODEL_UNREPORTED,
    SCHEMA_VERSION as AGGREGATE_SCHEMA_VERSION,
    STATE_MEASURED as AGGREGATE_STATE_MEASURED,
    STATE_UNKNOWN as AGGREGATE_STATE_UNKNOWN,
    AggregateSource,
    build_atlas_aggregate,
    build_atlas_aggregate_dto,
    build_unattached_atlas_aggregate,
    is_sentinel_model_label,
    model_label,
    validate_atlas_aggregate,
)
from .calls import (
    CALLS_SCHEMA_VERSION,
    DEFAULT_LIMIT as CALLS_DEFAULT_LIMIT,
    MAX_LIMIT as CALLS_MAX_LIMIT,
    OUTCOME_FILTERS,
    SelfCallsRequest,
    build_self_calls_dto,
    call_id_for,
    validate_self_calls_dto,
)
from .dto import SCHEMA_VERSION, Provenance
from .errors import (
    AccessDenied,
    ContractError,
    InputLimitExceeded,
    InvalidBucket,
    InvalidRequest,
    InvalidWindow,
    UnverifiedSource,
)
from .identity import (
    CONTENT_CONSENT_ABSENT,
    CONTENT_CONSENT_GRANTED,
    CONTENT_CONSENT_STATES,
    CONTENT_CONSENT_WITHDRAWN,
    SCOPE_CALLS_SELF_READ,
    SCOPE_USAGE_GLOBAL_DIAGNOSTICS,
    SCOPE_USAGE_READ,
    SCOPE_USAGE_UNATTRIBUTED,
    AuthorizedScope,
    GatewayKeyBinding,
    KeyDirectory,
    OwnCallScope,
    RequesterContext,
    resolve_own_call_scope,
)
from .ingest import (
    MAX_REQUEST_ID_LEN,
    IngestLimits,
    IngestResult,
    RawEvent,
    ingest_jsonl,
)
from .service import (
    MAX_WINDOW_HOURS,
    AtlasRequest,
    build_atlas_dto,
    collect_authorized_records,
    expected_bucket_starts,
    validate_dto,
    validate_window,
)
from .stats import PERCENTILE_METHOD, percentile

__all__ = [
    "SCHEMA_VERSION",
    "AGGREGATE_SCHEMA_VERSION",
    "AGGREGATE_STATE_MEASURED",
    "AGGREGATE_STATE_UNKNOWN",
    "MAX_MODELS",
    "MODEL_LABEL_ESCAPE",
    "MODEL_OTHER",
    "MODEL_UNREPORTED",
    "AggregateSource",
    "UnverifiedSource",
    "is_sentinel_model_label",
    "build_atlas_aggregate",
    "build_atlas_aggregate_dto",
    "build_unattached_atlas_aggregate",
    "collect_authorized_records",
    "model_label",
    "validate_atlas_aggregate",
    "CALLS_SCHEMA_VERSION",
    "CALLS_DEFAULT_LIMIT",
    "CALLS_MAX_LIMIT",
    "PERCENTILE_METHOD",
    "MAX_REQUEST_ID_LEN",
    "MAX_WINDOW_HOURS",
    "OUTCOME_FILTERS",
    "AccessDenied",
    "AtlasRequest",
    "AuthorizedScope",
    "CONTENT_CONSENT_ABSENT",
    "CONTENT_CONSENT_GRANTED",
    "CONTENT_CONSENT_STATES",
    "CONTENT_CONSENT_WITHDRAWN",
    "ContractError",
    "GatewayKeyBinding",
    "IngestLimits",
    "IngestResult",
    "InputLimitExceeded",
    "InvalidBucket",
    "InvalidRequest",
    "InvalidWindow",
    "KeyDirectory",
    "OwnCallScope",
    "Provenance",
    "RawEvent",
    "RequesterContext",
    "SCOPE_CALLS_SELF_READ",
    "SCOPE_USAGE_GLOBAL_DIAGNOSTICS",
    "SCOPE_USAGE_READ",
    "SCOPE_USAGE_UNATTRIBUTED",
    "SelfCallsRequest",
    "build_atlas_dto",
    "build_self_calls_dto",
    "call_id_for",
    "expected_bucket_starts",
    "ingest_jsonl",
    "percentile",
    "resolve_own_call_scope",
    "validate_dto",
    "validate_self_calls_dto",
    "validate_window",
]
