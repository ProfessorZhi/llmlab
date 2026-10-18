"""OpenAPI 文档，**由路由表生成**。

这份文档不是手写的：它从 :data:`backend.evaluation.api.ROUTES` 推导，因此不可能出现
"文档里有、代码里没有"的端点。:func:`validate_openapi` 是反向的那一半检查——
文档里出现的每一条 path/method 也必须在路由表里。

请求体的字段清单同样只写一遍：``EvaluationSubmitRequest`` 的 properties 直接来自
:data:`backend.evaluation.service.REQUEST_FIELDS`，所以新增一个可提交字段时，
``additionalProperties: false`` 与 allowlist 会一起更新，不会各自漂移。

DTO 的 schema 是**声明式**的：它描述 ``dto.py`` 会产出的形状，而"产出的形状确实
如此"由 ``dto.validate_dto`` 在每次构建时保证。两者是同一件事的两个方向，
都在 ``tests/evaluation/test_dto_contract.py`` 里被断言。
"""

from __future__ import annotations

from typing import Mapping

from . import api
from .admission import all_reasons
from .dto import (
    MODULE_STATES,
    SCHEMA_VERSION,
    STATUS_LABELS,
)
from .models import JOB_STATUSES, METRIC_STATES
from .service import REQUEST_FIELDS

OPENAPI_VERSION = "3.1.0"

#: 本契约的 openapi 文档版本，与 DTO 的 schema_version 分开。
CONTRACT_VERSION = "1.0.0"


def _metric_sample() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "state", "value"],
        "properties": {
            "name": {"type": "string"},
            "state": {"type": "string", "enum": list(METRIC_STATES)},
            # 关键约束：未测到的指标必须是 null，不能是 0。
            "value": {
                "oneOf": [{"type": "number"}, {"type": "null"}],
                "description": "state != 'known' 时必须为 null；0 是测量结果，不是缺失。",
            },
            "unit": {"type": ["string", "null"]},
            "note": {"type": ["string", "null"]},
        },
    }


def _report_reference() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["state", "available", "content_read"],
        "properties": {
            "ref": {
                "type": ["string", "null"],
                "description": "相对、只读安全的引用；未授权或未接线时为 null。",
            },
            "state": {
                "type": "string",
                "enum": ["known", "unknown", "not_configured", "unavailable"],
            },
            "available": {"type": "boolean"},
            # 本部署的固定立场：内容不经这个面出去。
            "content_read": {"type": "boolean", "const": False},
            "content_read_reason": {"type": "string"},
            "reference_sha256": {"type": ["string", "null"]},
            "reason": {"type": ["string", "null"]},
            "note": {"type": ["string", "null"]},
        },
    }


def _schemas() -> dict:
    return {
        "Envelope": {
            "type": "object",
            "required": [
                "schema_version",
                "mode",
                "source",
                "synthetic",
                "data",
                "scope",
                "notes",
                "display_hints",
            ],
            "properties": {
                "schema_version": {"type": "string", "const": SCHEMA_VERSION},
                "mode": {"type": "string", "enum": ["synthetic", "live"]},
                "source": {"type": "string"},
                # 没有默认值：合成数据必须在每一份响应里自我声明。
                "synthetic": {"type": "boolean"},
                "data": {},
                "scope": {"$ref": "#/components/schemas/ScopeBlock"},
                "notes": {"type": "array", "items": {"type": "string"}},
                "display_hints": {"type": "array", "items": {"type": "string"}},
            },
        },
        "ScopeBlock": {
            "type": "object",
            "properties": {
                "principal": {"type": ["string", "null"]},
                "uid": {"type": ["integer", "null"]},
                "linux_user": {"type": ["string", "null"]},
                "scopes": {"type": "array", "items": {"type": "string"}},
                "roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "仅展示与审计；不产生任何权限。",
                },
                "project_scope": {"type": "array", "items": {"type": "string"}},
                "is_admin_read": {"type": "boolean"},
                "is_admin_manage": {"type": "boolean"},
                "elevated_read": {"type": "boolean"},
            },
        },
        "JobStatus": {
            "type": "object",
            "required": ["job_id", "status", "terminal", "synthetic", "dispatch", "result"],
            "properties": {
                "job_id": {"type": "string", "pattern": "^evj-[0-9a-f]{24}$"},
                "owner": {"type": "string"},
                "owner_uid": {"type": "integer"},
                "project_id": {"type": "string"},
                "status": {"type": "string", "enum": list(JOB_STATUSES)},
                "status_label": {
                    "type": "string",
                    "enum": sorted(set(STATUS_LABELS.values())),
                },
                "terminal": {"type": "boolean"},
                "active": {"type": "boolean"},
                "cancellable": {"type": "boolean"},
                "retryable": {"type": "boolean"},
                "synthetic": {"type": "boolean"},
                "runner_kind": {"type": "string", "enum": ["mock", "evalscope_http", "evalscope_cli"]},
                "dispatch": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["not_dispatched", "dispatched", "refused"],
                        },
                        "reason": {"type": ["string", "null"]},
                        "dispatched": {"type": "boolean"},
                    },
                },
                "cancel_requested": {"type": "boolean"},
                "attempt": {"type": "integer", "minimum": 1},
                "retry_of": {"type": ["string", "null"]},
                "result": {"$ref": "#/components/schemas/ResultSummary"},
            },
        },
        "ResultSummary": {
            "type": "object",
            "required": ["available", "synthetic", "metrics"],
            "properties": {
                "available": {"type": "boolean"},
                "synthetic": {"type": ["boolean", "null"]},
                "version": {"type": ["integer", "null"]},
                "created_at": {"type": ["string", "null"]},
                "metrics": {
                    "type": "object",
                    "additionalProperties": {"$ref": "#/components/schemas/MetricSample"},
                },
                "report": {"$ref": "#/components/schemas/ReportReference"},
                "reproducibility": {"type": ["object", "null"]},
                "summary": {"type": ["string", "null"]},
                "empty_state": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/EmptyState"},
                        {"type": "null"},
                    ]
                },
            },
        },
        "EmptyState": {
            "type": "object",
            "description": "空态：value 恒为 null。空态不是 0，也不是失败。",
            "required": ["state", "value"],
            "properties": {
                "state": {"type": "string", "const": "unknown"},
                "label": {"type": "string"},
                "value": {"type": "null"},
                "what": {"type": "string"},
                "note": {"type": "string"},
            },
        },
        "MetricSample": _metric_sample(),
        "ReportReference": _report_reference(),
        "Admission": {
            "type": "object",
            "properties": {
                "allowed": {"type": "boolean"},
                "can_dispatch": {"type": "boolean"},
                "reason": {"type": ["string", "null"], "enum": [*all_reasons(), None]},
                "reasons": {"type": "array", "items": {"type": "string"}},
                "gpu": {"type": "object"},
                "runner": {"type": "object"},
                "window_open": {"type": "boolean"},
                "window": {"type": "object"},
                "synthetic_only": {"type": "boolean"},
                "explain": {"type": "string"},
            },
        },
        "ModuleGate": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": list(MODULE_STATES)},
                "reason": {"type": ["string", "null"]},
                "capabilities": {"type": "array", "items": {"type": "object"}},
                "blocking_count": {"type": "integer", "minimum": 0},
            },
        },
        "EvaluationSubmitRequest": {
            "type": "object",
            # 未知字段是 400，不是被忽略的噪声。
            "additionalProperties": False,
            "required": ["deployment_id", "eval_type"],
            "properties": {
                "deployment_id": {"type": "string", "maxLength": 128},
                "eval_type": {
                    "type": "string",
                    "enum": ["correctness", "throughput", "latency", "safety_preset"],
                },
                "dataset_slug": {
                    "type": "string",
                    "description": "必须命中允许清单；不在清单里即 400。",
                },
                "params": {
                    "type": "object",
                    "description": "键必须是该数据集声明的参数；值必须落在声明范围内。",
                    "additionalProperties": {"type": ["number", "boolean", "string", "null"]},
                },
                "preset": {
                    "type": "string",
                    "description": "仅当 eval_type=safety_preset 时给出；预设的参数不可覆盖。",
                },
                "idempotency_key": {"type": "string", "maxLength": 128},
            },
            "description": (
                "可提交字段是**封闭**的：命令、脚本路径、外部地址与正文数据类字段"
                "（command / args / path / url / prompt 等）一律拒绝整个请求。"
            ),
        },
        "ContractError": {
            "type": "object",
            "required": ["ok", "error"],
            "properties": {
                "ok": {"type": "boolean", "const": False},
                "error": {
                    "type": "object",
                    "properties": {
                        "contract_code": {"type": "string"},
                        "message": {
                            "type": ["string", "null"],
                            "description": "存储层错误不外发消息（内部结构与路径）。",
                        },
                        "schema_version": {"type": "string"},
                    },
                },
            },
        },
    }


def build_openapi_document(*, enable_mock_routes: bool = True) -> dict:
    """产出 OpenAPI 文档。

    默认包含开发专用路由（这样它能被文档化），但路由带 ``x-dev-only`` 标记，
    且 ``api.routes_for`` 在生产默认下不会注册它。
    """
    paths: dict[str, dict] = {}
    for route, operation in _operations(enable_mock_routes=enable_mock_routes):
        paths.setdefault(route.template, {})[route.method.lower()] = operation

    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "LLM Lab Portal — EvalScope 评测中心契约",
            "version": CONTRACT_VERSION,
            "description": (
                "平台层的评测任务契约：身份来自服务端可信目录，评测类型/数据集/参数走允许清单，"
                "自定义命令与外部地址被结构性排除，真实派发受 GPU 准入与管理员开关双重约束。"
                f"DTO schema_version = {SCHEMA_VERSION}。"
            ),
        },
        "servers": [{"url": "/"}],
        "tags": [{"name": "evaluation", "description": "EvalScope 评测中心"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "不透明凭据；由服务端可信目录解析为主体。"
                        "身份**不能**由请求自报（owner/uid/role 等字段与 header 一律拒绝）。"
                    ),
                }
            },
            "schemas": _schemas(),
        },
    }


def _operations(*, enable_mock_routes: bool) -> list[tuple[api.Route, dict]]:
    operations: list[tuple[api.Route, dict]] = []
    for route in api.routes_for(enable_mock_routes=enable_mock_routes):
        operation = route.to_openapi()
        if "{job_id}" in route.template:
            _with_job_id(operation)
        if "{report_ref}" in route.template:
            _with_report_ref(operation)
        operations.append((route, operation))
    return operations


def _with_job_id(operation: dict) -> dict:
    operation.setdefault("parameters", []).insert(0, dict(api._JOB_ID_PARAM))
    return operation


#: 不透明报告引用的路径参数。形状与 ``provenance.opaque_report_ref`` 一致：它**不是**
#: 一条路径，因此这里没有 ``format: path``——只有一个受限的 id 形状。
_REPORT_REF_PARAM = {
    "name": "report_ref",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "pattern": "^evrp-[0-9a-f]{24}$"},
}


def _with_report_ref(operation: dict) -> dict:
    operation.setdefault("parameters", []).insert(0, dict(_REPORT_REF_PARAM))
    return operation


def validate_openapi(document: Mapping[str, object]) -> None:
    """检查文档与路由表一致（双向）。不一致抛 ``AssertionError``。"""
    if not isinstance(document, Mapping):
        raise AssertionError("an OpenAPI document must be a mapping")
    if document.get("openapi") != OPENAPI_VERSION:
        raise AssertionError("unexpected OpenAPI version")

    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise AssertionError("the document has no paths")

    expected = {(route.template, route.method.lower()) for route in api.ROUTES}
    documented = {
        (path, method)
        for path, methods in paths.items()
        for method in methods  # type: ignore[union-attr]
    }

    missing = sorted(expected - documented)
    if missing:
        raise AssertionError(f"routes missing from the document: {missing}")
    extra = sorted(documented - expected)
    if extra:
        raise AssertionError(f"documented operations with no route: {extra}")

    # 请求体 schema 的字段必须与 service 的 allowlist 完全一致。
    props = (
        document["components"]["schemas"]["EvaluationSubmitRequest"]["properties"]  # type: ignore[index]
    )
    if set(props) != set(REQUEST_FIELDS):
        raise AssertionError(
            "EvaluationSubmitRequest drifted from service.REQUEST_FIELDS: "
            f"{sorted(set(props) ^ set(REQUEST_FIELDS))}"
        )

    schemas = document["components"]["schemas"]  # type: ignore[index]
    for required in ("Envelope", "JobStatus", "ResultSummary", "EmptyState", "MetricSample"):
        if required not in schemas:
            raise AssertionError(f"the document is missing schema {required!r}")


__all__ = [
    "CONTRACT_VERSION",
    "OPENAPI_VERSION",
    "build_openapi_document",
    "validate_openapi",
]
