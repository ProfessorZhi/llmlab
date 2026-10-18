"""对外 DTO 的形状：纯 JSON 可序列化的嵌套 dict。

沿用仓库既有做法（``backend.registry.dto`` / ``backend.atlas.dto``）：

* 输出是**纯 dict**，便于 ``json.dumps(..., sort_keys=True)`` 做逐字节回归；
* 顶层带 ``schema_version`` / ``mode`` / ``source`` / ``synthetic``，且 ``synthetic``
  **没有**默认值——调用方必须显式声明，避免把合成数据说成生产数据；
* 测不到的东西是 ``None`` + 一个状态字符串，不是 ``0``。

本模块**只负责形状**，不查库、不判权。可见性由调用方以显式布尔量传入，这样
「谁看得见什么」就只有 ``authz.py`` 一个地方说了算。

V6 前端契约
-----------
``prototypes/v6`` 的评测中心里有两句话是硬约束，本模块的字段就是为它们准备的：

* "尚无经授权读取的真实评测记录 …… 这不是评测失败，也不代表已有模型得分为 0。"
  -> :func:`render_result_summary` 在无结果时返回 ``empty_state``，其 ``value`` 是
  ``None``、``state`` 是 ``unknown``，**永远不是 0**。
* "未开放时这里说明原因，而不是给一个点了没反应的按钮。"
  -> :func:`render_module_gate` 的每个能力都带 ``state`` + ``reason``。

:func:`validate_dto` 是这一层的自检：它在**每次**构建 DTO 时检查两件事——
「状态不是 known 的指标不得带数值」以及「DTO 里不得出现正文类字段」。
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping

from .admission import AdmissionDecision
from .identity import RequesterContext
from .models import (
    EvalJob,
    EvalResult,
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    isoformat,
)
from .provenance import (
    REASON_CONTENT_DISABLED,
    REASON_DIMENSION_NOT_REPORTED,
    REASON_DIMENSION_REPORTED_UNKNOWN,
    REASON_REFERENCE_NOT_AUTHORIZED,
    REPORT_DIMENSIONS,
    SOURCE_LABELS,
    safe_text,
)
from .reports import ReportReference, ReportRoot

#: DTO 模式版本。字段增删必须同步 ``docs/evalscope-center.md``。
SCHEMA_VERSION = "evaluation/1"

#: 部署形态。``synthetic`` 是本仓库此刻唯一诚实的取值：没有生产部署。
MODE_SYNTHETIC = "synthetic"
MODE_LIVE = "live"
MODES = (MODE_SYNTHETIC, MODE_LIVE)

#: 顶层必需字段。``validate_dto`` 每次都会核对。
REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "mode",
    "source",
    "synthetic",
    "data",
    "scope",
    "notes",
    "display_hints",
)

#: **永远不能**出现在 DTO 里的键。DTO 是给浏览器的，正文类数据不该到那里。
FORBIDDEN_DTO_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "completion",
        "completions",
        "messages",
        "content",
        "text",
        "image",
        "images",
        "cv",
        "payload",
        "weight_path",
        "model_path",
        "command",
        "args",
        "env",
        "path",
        "file_path",
        "usage",
        "usage_jsonl",
    }
)

#: 状态 -> 展示标签。UI 直接渲染，不在前端拼字符串。
STATUS_LABELS = {
    STATUS_QUEUED: "排队中",
    STATUS_RUNNING: "运行中",
    STATUS_COMPLETED: "已完成",
    STATUS_FAILED: "失败",
    STATUS_CANCELED: "已取消",
}

#: 模块门（control plane）状态取值域。与 ``portal-modules/1`` 保持一致。
MODULE_AVAILABLE = "available"
MODULE_UNAVAILABLE = "unavailable"
MODULE_DISABLED = "disabled"
MODULE_UNKNOWN = "unknown"
MODULE_STATES = (MODULE_AVAILABLE, MODULE_UNAVAILABLE, MODULE_DISABLED, MODULE_UNKNOWN)

# 模块门原因。前三个沿用 ``portal-modules/1`` 的措辞。
REASON_NO_SESSION = "no_authenticated_session"
REASON_MISSING_SCOPE = "missing_required_scope"
REASON_PREVIEW_NO_BACKEND = "preview_build_has_no_real_backend"
REASON_DISPATCH_FORBIDDEN = "real_dispatch_forbidden"
REASON_REPORT_ROOT_UNCONFIGURED = "report_root_not_configured"

#: 无结果时的空态。刻意**不是** 0。
EMPTY_STATE_LABEL = "等待数据接入"


class DtoError(ValueError):
    """DTO 偏离了自己的字段清单。构建期错误，不是请求期错误。"""


def envelope(
    data: object,
    *,
    mode: str,
    source: str,
    notes: Iterable[str] | None = None,
    display_hints: Iterable[str] | None = None,
    scope: dict | None = None,
    extra_meta: dict | None = None,
) -> dict:
    """标准响应体：``data`` + 契约元信息。"""
    if mode not in MODES:
        raise DtoError(f"unknown evaluation mode: {mode!r}")
    if not source:
        raise DtoError("a DTO must declare its source")
    synthetic = mode == MODE_SYNTHETIC
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "source": source,
        "synthetic": synthetic,
        "data": data,
        "scope": scope,
        "notes": list(notes or ()),
        "display_hints": list(display_hints or ()),
    }
    if extra_meta:
        payload["meta"] = dict(extra_meta)
    if synthetic:
        # 合成形态必须在**每一份**响应里自我声明，而不是只在文档里写一句。
        payload["display_hints"] = [
            "SYNTHETIC: 本部署非生产，数据不代表真实运行状态。",
            *payload["display_hints"],
        ]
    return payload


def scope_block(
    requester: RequesterContext | None,
    *,
    projects: Iterable[str] = (),
    elevated_read: bool = False,
) -> dict:
    """审计用的授权快照。``roles`` 只展示，不参与判定。"""
    if requester is None:
        return {
            "principal": None,
            "uid": None,
            "linux_user": None,
            "scopes": [],
            "roles": [],
            "project_scope": [],
            "is_admin_read": False,
            "is_admin_manage": False,
            "elevated_read": False,
        }
    return {
        "principal": requester.principal,
        "uid": requester.uid,
        "linux_user": requester.linux_user,
        "scopes": sorted(requester.scopes),
        "roles": sorted(requester.roles),
        "project_scope": sorted(projects),
        "is_admin_read": requester.is_admin_read,
        "is_admin_manage": requester.is_admin_manage,
        "elevated_read": bool(elevated_read),
    }


def params_hash(job: EvalJob) -> str:
    """可复现性指纹：评测类型 + 数据集 + 版本 + 参数。

    算的是**规范化后的**规格（参数已被 allowlist 补齐并排序），因此同一个请求两次
    提交得到同一个指纹，而"看起来一样、其实参数不同"的两次提交不会撞在一起。
    """
    payload = {
        "eval_type": job.spec.eval_type,
        "dataset_slug": job.spec.dataset_slug,
        "dataset_version": job.spec.dataset_version,
        "preset": job.spec.preset,
        "params": dict(job.spec.params),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reproducibility_block(job: EvalJob) -> dict:
    """报告要能被复现所需要的那几个字段。"""
    return {
        "eval_type": job.spec.eval_type,
        "dataset_slug": job.spec.dataset_slug,
        "dataset_version": job.spec.dataset_version,
        "preset": job.spec.preset,
        "params": dict(job.spec.params),
        "params_hash": params_hash(job),
        "target_deployment_id": job.target.deployment_id,
        "runner_kind": job.runner_kind,
        "attempt": job.attempt,
    }


def empty_state_block(*, what: str) -> dict:
    """一个诚实的空态。**不是** 0。"""
    return {
        "state": "unknown",
        "label": EMPTY_STATE_LABEL,
        "value": None,
        "what": what,
        "note": "这不是评测失败，也不代表已有模型得分为 0。",
    }


def render_result_summary(
    result: EvalResult | None,
    *,
    include_report_ref: bool,
    report: ReportReference | None = None,
) -> dict:
    """一次任务的结果摘要。``result is None`` -> 空态，而不是一份全 0 的报告。"""
    if result is None:
        return {
            "available": False,
            "synthetic": None,
            "empty_state": empty_state_block(what="evaluation result"),
            "version": None,
            "created_at": None,
            "metrics": {},
            "reproducibility": None,
            "report": None,
        }

    metrics = {name: sample.to_dict() for name, sample in sorted(result.metrics.items())}
    report_block = None
    if include_report_ref:
        if report is not None:
            report_block = report.to_dict()
        elif result.report_ref:
            # 引用存在但调用者没有报告权限：回一个"有引用、不可见"的形状，
            # 而不是回 null——那会让 UI 分不清"没有报告"和"你没权限看这份报告"。
            report_block = {
                "ref": None,
                "state": "unavailable",
                "available": False,
                "content_read": False,
                "content_read_reason": "report_reference_not_authorized",
                "reference_sha256": None,
                "reason": "report_reference_not_authorized",
                "note": "本报告存在，但读取它的引用需要单独的授权。",
            }
        else:
            report_block = {
                "ref": None,
                "state": "unknown",
                "available": False,
                "content_read": False,
                "content_read_reason": "no_report_referenced",
                "reference_sha256": None,
                "reason": "no_report_referenced",
                "note": "这个结果没有附带报告引用。",
            }

    return {
        "available": True,
        "synthetic": result.synthetic,
        "empty_state": None,
        "version": result.version,
        "created_at": isoformat(result.created_at),
        "metrics": metrics,
        "reproducibility": None,  # 由 render_job_status 填充（它才知道 job）
        "report": report_block,
        "summary": result.summary,
    }


def render_job_status(
    job: EvalJob,
    *,
    result: EvalResult | None = None,
    include_report_ref: bool = False,
    report: ReportReference | None = None,
) -> dict:
    """一个任务的完整状态视图。

    **报告引用只来自调用方显式传入的 ``report``。** 这个函数不会自己去解析
    ``result.report_ref``：曾经有一个"顺手补上"的回退分支，它会在调用者**没有**报告
    权限时把引用渲染出来——一个授权判断被一个便利分支悄悄绕过去的典型例子。
    现在没有任何代码路径能在没有 ``report`` 的情况下产出引用。
    """
    summary = render_result_summary(
        result, include_report_ref=include_report_ref, report=report
    )
    if summary["available"]:
        summary["reproducibility"] = reproducibility_block(job)

    return {
        "job_id": job.job_id,
        "owner": job.owner_principal,
        "owner_uid": job.owner_uid,
        "project_id": job.project_id,
        "status": job.status,
        "status_label": STATUS_LABELS.get(job.status, job.status),
        "terminal": job.terminal,
        "active": job.active,
        "cancellable": job.cancellable,
        "retryable": job.retryable,
        # 任务级的合成标记：本部署里 mock 是唯一 runner，因此这里恒为 true。
        "synthetic": job.synthetic,
        "runner_kind": job.runner_kind,
        "dispatch": {
            "state": job.dispatch_state,
            "reason": job.dispatch_reason,
            "dispatched": job.dispatch_state == "dispatched",
        },
        "cancel_requested": job.cancel_requested,
        "attempt": job.attempt,
        "retry_of": job.retry_of,
        "target": job.target.to_dict(),
        "result": summary,
        "created_at": isoformat(job.created_at),
        "updated_at": isoformat(job.updated_at),
        "started_at": isoformat(job.started_at),
        "finished_at": isoformat(job.finished_at),
        "failure": (
            {"code": job.failure_code, "detail": job.failure_detail}
            if job.failure_code or job.failure_detail
            else None
        ),
    }


def render_job_list(jobs: Iterable[EvalJob], *, total_visible: int | None = None) -> dict:
    rendered = [render_job_status(job) for job in jobs]
    return {
        "jobs": rendered,
        "returned": len(rendered),
        # ``total_visible`` 是"这个调用者可见的总数"，与"本页条数"不是一回事。
        "total_visible": total_visible if total_visible is not None else len(rendered),
        "empty_state": None if rendered else empty_state_block(what="evaluation jobs"),
    }


def explain_admission(decision: AdmissionDecision) -> str:
    """一句话解释准入结论。UI 直接渲染，不在前端拼字符串。"""
    if decision.allowed:
        return "真实评测派发已允许。"
    return "真实评测派发被禁止；任务只进入排队，本部署只产出合成样本。"


def render_admission(decision: AdmissionDecision) -> dict:
    """准入视图：告诉运维"要让它跑起来还差哪几件事"。"""
    payload = decision.to_dict()
    payload["can_dispatch"] = decision.allowed
    payload["explain"] = explain_admission(decision)
    return payload


def render_module_gate(
    *,
    requester: RequesterContext | None,
    admission: AdmissionDecision,
    report_root: ReportRoot | None = None,
    native_entry_available: bool = False,
) -> dict:
    """V6 评测中心「EvalScope 入口授权」面板的控制面数据。

    ``portal-modules/1`` 的措辞在这里被复用（``available`` / ``unavailable`` /
    ``disabled`` / ``unknown`` 与原因字符串），因此前端可以用同一套渲染逻辑。
    """
    capabilities: list[dict] = []

    if requester is None:
        return {
            "state": MODULE_UNKNOWN,
            "reason": REASON_NO_SESSION,
            "capabilities": [],
            "note": "尚未确认会话；在确认之前不声称任何能力。",
        }

    readable = requester.has_scope("evaluation:read")
    submittable = requester.has_scope("evaluation:submit")

    capabilities.append(
        {
            "name": "evaluation.jobs.read",
            "state": MODULE_AVAILABLE if readable else MODULE_UNAVAILABLE,
            "reason": None if readable else REASON_MISSING_SCOPE,
            "note": "读取你名下的评测任务与结果。",
        }
    )
    capabilities.append(
        {
            "name": "evaluation.jobs.submit",
            "state": MODULE_AVAILABLE if submittable else MODULE_UNAVAILABLE,
            "reason": None if submittable else REASON_MISSING_SCOPE,
            "note": "提交进入队列；是否真的执行由准入决定。",
        }
    )
    capabilities.append(
        {
            "name": "evaluation.dispatch.real",
            "state": MODULE_AVAILABLE if admission.allowed else MODULE_UNAVAILABLE,
            "reason": None if admission.allowed else REASON_DISPATCH_FORBIDDEN,
            "note": explain_admission(admission),
            "reasons": list(admission.reasons),
        }
    )
    capabilities.append(
        {
            "name": "evalscope.native_ui",
            "state": MODULE_AVAILABLE if native_entry_available else MODULE_DISABLED,
            "reason": None if native_entry_available else REASON_PREVIEW_NO_BACKEND,
            "note": (
                "原生 EvalScope 界面未开放：子路径代理与身份边界尚未接线。"
                if not native_entry_available
                else "原生界面已开放。"
            ),
        }
    )

    root = report_root if report_root is not None else ReportRoot(None)
    capabilities.append(
        {
            "name": "evaluation.report.reference",
            "state": MODULE_AVAILABLE if root.configured else MODULE_UNAVAILABLE,
            "reason": None if root.configured else REASON_REPORT_ROOT_UNCONFIGURED,
            "note": (
                "报告引用可解析。" if root.configured else "报告根目录未接线，引用不会被解析成路径。"
            ),
        }
    )

    blocking = [cap for cap in capabilities if cap["state"] != MODULE_AVAILABLE]
    overall = MODULE_AVAILABLE if readable else MODULE_UNAVAILABLE
    return {
        "state": overall,
        "reason": None if readable else REASON_MISSING_SCOPE,
        "capabilities": capabilities,
        "blocking_count": len(blocking),
        "note": (
            "数据面可用；带原因的能力是**未开放**，不是无响应。"
            if readable
            else "缺少 evaluation:read，模块目录对本主体不开放。"
        ),
    }


def render_monitoring(
    *,
    counts: Mapping[str, int],
    requester: RequesterContext | None,
    admission: AdmissionDecision,
    report_root: ReportRoot | None = None,
    last_result_at: str | None = None,
    results_total: int | None = None,
) -> dict:
    """监控面聚合。计得到的计数是数字，测不到的显存是 ``None``。"""
    # 未知的状态也列出来，且值为 0 —— 这里的 0 是**真的 0**（数过，一个都没有），
    # 与"没测到"不同，因此它是合法数字。
    by_status = {status: int(counts.get(status, 0)) for status in STATUS_LABELS}
    root = report_root if report_root is not None else ReportRoot(None)
    return {
        "jobs_by_status": by_status,
        "jobs_total": sum(by_status.values()),
        "results_total": results_total,
        "last_result_at": last_result_at,
        "gpu": admission.gpu.to_dict(),
        "dispatch": {
            "allowed": admission.allowed,
            "reasons": list(admission.reasons),
            "synthetic_only": admission.synthetic_only,
        },
        "report_root": root.describe(),
        "scope": scope_block(requester),
        "quality": {
            # 这些是"量不出来"的，因此是 None + state，不是 0。
            "gpu_free_mib": None if not admission.gpu.observed else admission.gpu.free_mib,
            "real_evaluation_results": 0,  # 本部署确实一个都没有，且这是数过的事实
            "measured_over": "synthetic_only" if admission.synthetic_only else "real_runs",
        },
        "empty_state": empty_state_block(what="monitoring aggregate")
        if not by_status.get(STATUS_COMPLETED)
        else None,
    }


# ---------------------------------------------------------------------------
# 报告目录（EvalScope / 旧 Benchmark 分源）
#
# 这一组渲染器把 ``report_catalog`` 的结论摆成前端契约。两条不可让步的形状：
#
# * ``source`` 是**来源适配器盖的章**，不是从负载里读出来的。因此 DTO 里写着
#   ``evalscope`` 的那一行，一定来自 EvalScope 适配器。
# * 缺失的维度渲染成 ``state=unknown, value=null``，**永远不是 0**——"这次没测到"
#   与"测得 0"在评测语境里不是一回事。
# ---------------------------------------------------------------------------

#: 报告目录里"确实是 0 份"的标签。与 :data:`EMPTY_STATE_LABEL`（未知）刻意区分。
COUNTED_ZERO_LABEL = "0 份报告"
#: 措辞刻意是**主体范围**的：来源里可能有别人的报告，那与"你有几份"无关，
#: 也不能从这条空态里被推断出来。
COUNTED_ZERO_NOTE = "对你可见的报告为 0 份；这是一个围绕你自己的计数，不代表该来源里没有他人的报告。"


def report_access_block(*, authorized: bool) -> dict:
    """预览/下载的**授权结论**。内容字节从不在这里出现。

    ``content_read`` 恒为 ``False``：本部署的报告内容读取是关闭的（``reports.py`` 的
    ``read_bytes`` 一律 ``NotConfigured``）。授权只说明"允许你发起这次读取"，不说明
    字节会从这个面出去。
    """
    return {
        "authorized": bool(authorized),
        "content_read": False,
        "reason": REASON_CONTENT_DISABLED if authorized else REASON_REFERENCE_NOT_AUTHORIZED,
    }


def _missing_dimension_reason(record, name: str) -> str:
    """一个**没有已知取值**的维度，缺的到底是哪一环。

    来源"提到了但没给值"与"根本没提"分开：前者是上游测量缺了一环，后者是契约上就
    没覆盖。两者都**不是 0**，因此都在 ``missing_dimensions`` 里。
    """
    sample = record.dimensions.get(name)
    if sample is not None:
        return sample.note or REASON_DIMENSION_REPORTED_UNKNOWN
    return REASON_DIMENSION_NOT_REPORTED


def render_report_entry(
    entry,
    *,
    visibility: str,
    preview_authorized: bool,
    download_authorized: bool,
    include_content_bytes: bool = False,
) -> dict:
    """一条目录报告 -> 前端契约。

    ``include_content_bytes`` 存在只是为了在**构造期**挡住一次"顺手把正文塞进 DTO"的
    改动：这个面永远不携带正文，因此传 ``True`` 直接是编程错误。
    """
    if include_content_bytes:  # pragma: no cover - 结构性防线
        raise DtoError("the report DTO never carries report content")
    record = entry.record
    dimensions = {name: entry.dimension(name).to_dict() for name in REPORT_DIMENSIONS}
    missing = [
        {"name": name, "reason": _missing_dimension_reason(record, name)}
        for name in entry.missing_dimensions
    ]
    return {
        # **不透明**引用：由 (source, 上游 id, 相对引用) 摘要而来，不是路径。
        "report_ref": entry.ref_id,
        "source": record.source,
        "source_label": SOURCE_LABELS[record.source],
        "owner_state": "unowned" if record.owner is None else "owned",
        "owner": record.owner.to_dict() if record.owner is not None else None,
        "visibility": visibility,
        "synthetic": record.synthetic,
        "title": safe_text(record.title),
        "observed_at": isoformat(record.observed_at),
        "created_at": isoformat(record.created_at),
        "stale": entry.stale,
        "source_state": entry.source_state,
        "dimensions": dimensions,
        "missing_dimensions": missing,
        "preview": report_access_block(authorized=preview_authorized),
        "download": report_access_block(authorized=download_authorized),
    }


def counted_zero_block(*, what: str, sources: Iterable[str] = ()) -> dict:
    """一个**真的数过**的 0。与 :func:`empty_state_block`（未知）不是一回事。"""
    return {
        "state": "empty",
        "label": COUNTED_ZERO_LABEL,
        "value": 0,
        "what": what,
        "sources": sorted(sources),
        "note": COUNTED_ZERO_NOTE,
    }


def render_report_sources(status_rows: Iterable[Mapping[str, object]]) -> list[dict]:
    """把**主体范围内**的来源状态（``ReportCatalog.scoped_source_status``）摆成前端契约。

    每一行必须已经带上 ``visible_count``。这里刻意**不**接受一份全局计数去合并——
    那正是"状态从全局泄漏"得以重新发生的地方。
    """
    return [dict(row) for row in status_rows]


def render_report_catalog(
    *,
    reports: Iterable[dict],
    sources: Iterable[dict],
    include_unowned: bool,
    empty_state: dict | None,
) -> dict:
    rendered = list(reports)
    return {
        "reports": rendered,
        "returned": len(rendered),
        "include_unowned": bool(include_unowned),
        "sources": list(sources),
        "empty_state": empty_state,
    }


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------


def _walk(node: object, path: str = "$") -> Iterable[tuple[str, object]]:
    yield path, node
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def validate_dto(payload: Mapping[str, object]) -> None:
    """核对一份 DTO 是否守住了本模块的两条不变量。

    1. 顶层必需字段齐全，``synthetic`` 是布尔量。
    2. 任何带 ``state`` 的块：``state != "known"`` 时 ``value`` **必须**是 ``None``。
       这是"没测到"与"等于 0"之间唯一的一道程序化防线。
    3. 任何位置都不出现正文类键（:data:`FORBIDDEN_DTO_KEYS`）。
    """
    if not isinstance(payload, Mapping):
        raise DtoError("a DTO must be a mapping")
    missing = [field for field in REQUIRED_TOP_LEVEL_FIELDS if field not in payload]
    if missing:
        raise DtoError(f"DTO is missing required field(s): {', '.join(missing)}")
    if not isinstance(payload["synthetic"], bool):
        raise DtoError("DTO synthetic must be an explicit boolean")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise DtoError(f"DTO schema_version must be {SCHEMA_VERSION!r}")

    for path, node in _walk(payload):
        # 键名来自**路径的最后一段**，而不是节点自身的类型：一个禁止出现的键可能挂在一个
        # 标量上（``{"prompt": "..."}``），如果只在 dict 节点上检查，它就漏过去了。
        key = path.rsplit(".", 1)[-1]
        if key in FORBIDDEN_DTO_KEYS:
            raise DtoError(f"DTO must not carry {key!r} (at {path})")

        if isinstance(node, Mapping) and "state" in node and "value" in node:
            state = node["state"]
            if state in ("unknown", "not_configured", "unavailable") and node["value"] is not None:
                raise DtoError(
                    f"an unmeasured value must be null, not {node['value']!r} (at {path})"
                )
    return None


__all__ = [
    "COUNTED_ZERO_LABEL",
    "COUNTED_ZERO_NOTE",
    "EMPTY_STATE_LABEL",
    "FORBIDDEN_DTO_KEYS",
    "MODES",
    "MODE_LIVE",
    "MODE_SYNTHETIC",
    "MODULE_AVAILABLE",
    "MODULE_DISABLED",
    "MODULE_STATES",
    "MODULE_UNAVAILABLE",
    "MODULE_UNKNOWN",
    "REASON_DISPATCH_FORBIDDEN",
    "REASON_MISSING_SCOPE",
    "REASON_NO_SESSION",
    "REASON_PREVIEW_NO_BACKEND",
    "REASON_REPORT_ROOT_UNCONFIGURED",
    "REQUIRED_TOP_LEVEL_FIELDS",
    "SCHEMA_VERSION",
    "STATUS_LABELS",
    "DtoError",
    "counted_zero_block",
    "empty_state_block",
    "envelope",
    "explain_admission",
    "params_hash",
    "render_admission",
    "render_job_list",
    "render_job_status",
    "render_module_gate",
    "render_monitoring",
    "render_report_catalog",
    "render_report_entry",
    "render_report_sources",
    "render_result_summary",
    "report_access_block",
    "reproducibility_block",
    "scope_block",
    "validate_dto",
]
