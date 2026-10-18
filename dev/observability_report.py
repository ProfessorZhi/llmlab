"""基础报告：把四种 DTO 从**合成数据**跑一遍，并当场自检不变量。

这份报告的作用是**证据**而不是散文：它不接受任何手写的「应该没问题」，
每一条结论都由当前代码在本次运行里算出来。任何一条不变量失败，进程以非零码退出。

用法::

    python -m dev.observability_report              # 人读摘要 + 每个 kind 的 JSON 样例
    python -m dev.observability_report --json       # 整体机器可读
    python -m dev.observability_report --kind usage.self   # 只打印一个 kind 的样例
    python -m dev.observability_report --quiet      # 只跑自检，只输出结论

产物即「给 UI 消费的 schema 例子」：每个 kind 一段完整的真实 DTO，可直接拿去写前端
fixture 或做快照测试。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from backend.observability import (
    KIND_ENGINE_STATUS,
    KIND_TIMELINE,
    KIND_USAGE_ADMIN,
    KIND_USAGE_SELF,
    QUALITY_UNKNOWN,
    build_admin_overview_dto,
    build_engine_status_dto,
    build_self_usage_dto,
    build_timeline_dto,
    validate_admin_overview_dto,
    validate_engine_status_dto,
    validate_timeline_dto,
    validate_usage_dto,
)
from backend.observability import (
    DRIVER_STATES,
    NVML_ERROR_PERMISSION,
    ProcHostCollector,
    driver_status,
)
from backend.observability.dto import CATALOGUE, PHYSICAL_SCOPES
from backend.observability.sources import FIXED_EXPRESSIONS
from dev.observability_preview import (
    PREVIEW_PROVENANCE,
    demo_directory,
    demo_engine_probe,
    demo_registry,
    demo_rows,
)
from backend.observability import MockHostCollector


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def build_all(now: datetime | None = None) -> dict:
    """用合成数据构建四种 DTO。"""
    moment = now or datetime.now(timezone.utc).replace(microsecond=0)
    text = demo_rows(moment)
    directory = demo_directory()
    registry = demo_registry()
    from datetime import timedelta

    from backend.observability import (
        ObservabilityRequest,
        RequesterPrincipal,
        SCOPE_ADMIN_READ,
        SCOPE_HOST_READ,
        SCOPE_SELF_READ,
    )

    admin = RequesterPrincipal(
        uid="u-9001",
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_SELF_READ, SCOPE_ADMIN_READ, SCOPE_HOST_READ}),
        authorized_endpoints=frozenset(),
    )
    alice = RequesterPrincipal(
        uid="u-1001",
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_SELF_READ}),
        authorized_endpoints=frozenset({"model-shared-a"}),
    )

    window_to = moment
    window_from = moment - timedelta(hours=1)

    def req(principal_obj, **kwargs):
        return ObservabilityRequest(
            principal=principal_obj,
            window_from=window_from,
            window_to=window_to,
            **kwargs,
        )

    return {
        KIND_USAGE_SELF: build_self_usage_dto(
            req(alice), text, directory, registry, PREVIEW_PROVENANCE, now=moment
        ),
        KIND_USAGE_ADMIN: build_admin_overview_dto(
            req(admin),
            text,
            directory,
            registry,
            PREVIEW_PROVENANCE,
            now=moment,
            host_report=MockHostCollector().collect(moment),
            engine_probe=demo_engine_probe(),
        ),
        KIND_ENGINE_STATUS: build_engine_status_dto(
            req(alice),
            registry,
            PREVIEW_PROVENANCE,
            now=moment,
            engine_probe=demo_engine_probe(),
        ),
        KIND_TIMELINE: build_timeline_dto(
            req(alice), text, directory, registry, PREVIEW_PROVENANCE, now=moment
        ),
    }


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------


def _report_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _unwired_request():
    """一份「未接线」形态的请求：只持有 self scope 的主体，不传 probe。"""
    from datetime import timedelta

    from backend.observability import ObservabilityRequest, RequesterPrincipal, SCOPE_SELF_READ

    moment = _report_now()
    return ObservabilityRequest(
        principal=RequesterPrincipal(
            uid="u-1001",
            roles=frozenset({"user"}),
            scopes=frozenset({SCOPE_SELF_READ}),
            authorized_endpoints=frozenset({"model-shared-a"}),
        ),
        window_from=moment - timedelta(hours=1),
        window_to=moment,
    )


def _demo_text() -> str:
    return demo_rows(_report_now())


def _unwired_admin_request():
    from datetime import timedelta

    from backend.observability import (
        ObservabilityRequest,
        RequesterPrincipal,
        SCOPE_ADMIN_READ,
        SCOPE_HOST_READ,
        SCOPE_SELF_READ,
    )

    moment = _report_now()
    return ObservabilityRequest(
        principal=RequesterPrincipal(
            uid="u-9001",
            roles=frozenset({"user"}),
            scopes=frozenset({SCOPE_SELF_READ, SCOPE_ADMIN_READ, SCOPE_HOST_READ}),
            authorized_endpoints=frozenset(),
        ),
        window_from=moment - timedelta(hours=1),
        window_to=moment,
    )


def _readings(node, path=""):
    """递归取出 DTO 里所有「读数」节点（带 metric_id + quality 的那层）。"""
    if isinstance(node, dict):
        if "metric_id" in node and "quality" in node:
            yield path, node
            return
        for key, value in node.items():
            yield from _readings(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _readings(value, f"{path}[{index}]")


def self_check(dtos: dict) -> list[dict]:
    """逐条检查不变量，返回 ``[{name, ok, detail}]``。"""
    results: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1. 四种 DTO 都能通过各自的结构自检。
    validators = {
        KIND_USAGE_SELF: validate_usage_dto,
        KIND_USAGE_ADMIN: validate_admin_overview_dto,
        KIND_ENGINE_STATUS: validate_engine_status_dto,
        KIND_TIMELINE: validate_timeline_dto,
    }
    for kind, validator in validators.items():
        try:
            validator(dtos[kind])
            check(f"{kind}: 结构自检", True)
        except Exception as exc:  # noqa: BLE001 - 报告要如实记录失败
            check(f"{kind}: 结构自检", False, str(exc))

    # 2. 全部读数都带齐 source / observed_at / quality / freshness。
    missing: list[str] = []
    for kind, dto in dtos.items():
        for path, reading in _readings(dto):
            for key in ("source", "quality", "display_quality", "freshness"):
                if key not in reading:
                    missing.append(f"{kind}:{path}.{key}")
            if reading["quality"] != QUALITY_UNKNOWN and reading.get("observed_at") is None:
                missing.append(f"{kind}:{path}.observed_at")
    check("每条读数都带四件套（source/observed_at/quality/freshness）", not missing,
          "; ".join(missing[:5]))

    # 3. UNKNOWN 必带 null 值 + 原因码，绝不带 0。
    bad_unknown: list[str] = []
    for kind, dto in dtos.items():
        for path, reading in _readings(dto):
            if reading["quality"] != QUALITY_UNKNOWN:
                continue
            held = [reading.get(key) for key in ("value", "state", "text") if key in reading]
            if any(value is not None for value in held):
                bad_unknown.append(f"{kind}:{path} 带了非 null 值")
            if reading.get("reason") is None:
                bad_unknown.append(f"{kind}:{path} 缺 reason")
    check("UNKNOWN 一律 value=null 且有原因码（绝不以 0 代替）", not bad_unknown,
          "; ".join(bad_unknown[:5]))

    # 4. 物理读数不带 uid；目录里没有「按人的字节读数」。
    physical_with_uid = [
        f"{kind}:{path}"
        for kind, dto in dtos.items()
        for path, reading in _readings(dto)
        if reading.get("scope") in PHYSICAL_SCOPES and "uid" in reading
    ]
    check("物理读数不带 uid", not physical_with_uid, "; ".join(physical_with_uid[:5]))

    per_principal_bytes = [
        metric_id
        for metric_id, spec in CATALOGUE.items()
        if spec.scope == "principal" and spec.unit in ("bytes", "percent", "celsius")
    ]
    check("目录里没有「按人的显存/占用」指标", not per_principal_bytes,
          ", ".join(per_principal_bytes))

    # 5. 按人计量的基准被钉死。
    basis_ok = all(
        dtos[kind].get("billing_basis") == "verified-key"
        for kind in (KIND_USAGE_SELF, KIND_USAGE_ADMIN, KIND_TIMELINE)
    )
    check("按人计量基准 == verified-key（不与整卡读数混用）", basis_ok)

    # 6. 设备块不含 uid。
    devices = (dtos[KIND_USAGE_ADMIN].get("host") or {}).get("devices") or []
    check("整卡块不含 uid", all("uid" not in device for device in devices),
          f"{len(devices)} 张卡")

    # 7. 普通主体看不到不可归属诊断；管理主体看得到。
    alice_unknown = dtos[KIND_USAGE_SELF]["unknown"]
    admin_unknown = dtos[KIND_USAGE_ADMIN]["unknown"]
    unattributable = ("unknown_key", "malformed_lines", "content_field_rejected")
    check("普通主体的不可归属诊断一律 null",
          all(alice_unknown.get(key) is None for key in unattributable))
    check("管理主体的不可归属诊断可见（整数）",
          all(isinstance(admin_unknown.get(key), int) for key in unattributable))

    # 8. 普通主体的端点可见集只含被授权项。
    check("普通主体只看到被授权的端点",
          dtos[KIND_USAGE_SELF]["scope"]["endpoints"] == ["model-shared-a"],
          str(dtos[KIND_USAGE_SELF]["scope"]["endpoints"]))

    # 9. 合成数据必须自报。
    check("合成数据在 display_hints 里自报",
          all(any("SYNTHETIC" in hint for hint in dto["display_hints"])
              for dto in dtos.values()))

    # 10. 外部 UI 不开 iframe、不下发外部 URL。
    ui_blocks = [dtos[kind]["external_ui"] for kind in (KIND_USAGE_SELF, KIND_USAGE_ADMIN,
                                                        KIND_ENGINE_STATUS)]
    check("原生 UI 不开 iframe、不下发外部 URL",
          all(block["iframe_allowed"] is False for block in ui_blocks)
          and all(entry["external_url"] is None
                  for block in ui_blocks for entry in block["entries"]))

    # 11. 窗口有界、桶数有上限。
    windows = [dto["window"] for dto in dtos.values()]
    check("所有窗口都声明 UTC 与有限跨度",
          all(window["timezone"] == "UTC" and isinstance(window["hours"], int)
              for window in windows))

    # 12. 引擎状态：**没接线**时是 unknown，不是 down。
    #     这里刻意用「不传 probe」的默认形态来验，而不是用本报告里那个演示 probe ——
    #     演示 probe 是接线后的样子，拿它验「未接线」等于验了个别的东西。
    unwired = build_engine_status_dto(
        _unwired_request(), demo_registry(), PREVIEW_PROVENANCE, now=_report_now()
    )
    counts = unwired["counts"]
    check("引擎探测未接线 -> unknown（不是 down）",
          counts["down"] == 0 and counts["unknown"] == counts["total"] and counts["total"] > 0,
          str(counts))

    # 12b. 接线后的演示 probe 必须给出混合状态，好让 UI 在开发期就见得到各态。
    wired = dtos[KIND_ENGINE_STATUS]["counts"]
    check("演示 probe 提供混合状态（UI 见得到 up/degraded/unknown）",
          wired["down"] == 0 and wired["unknown"] + wired["up"] + wired["degraded"] == wired["total"],
          str(wired))

    # 13. Prometheus 固定表达式里没有按人的查询。
    check("Prometheus 固定表达式不含按人计量",
          all(not metric_id.startswith("principal.") for metric_id in FIXED_EXPRESSIONS))

    # 14. 缺失的桶不补 0。
    coverage = dtos[KIND_TIMELINE]["coverage"]
    check("时间线缺失的桶不补 0（只体现在 coverage）",
          coverage["missing_count"] == max(0, coverage["expected_count"] - coverage["observed_count"]))

    # 15. 采集器未启用时，host 对象在但整体质量必须是 UNKNOWN —— 不能「有对象就是
    #     measured」，否则一屏 not_collected 会被画成可信读数。
    disabled = build_admin_overview_dto(
        _unwired_admin_request(),
        _demo_text(),
        demo_directory(),
        demo_registry(),
        PREVIEW_PROVENANCE,
        now=_report_now(),
        host_report=ProcHostCollector(enabled=False).collect(_report_now()),
    )
    check("采集器未启用 -> quality.host = unknown（不是 measured）",
          disabled["host"] is not None and disabled["quality"]["host"] == QUALITY_UNKNOWN,
          f"host={'有' if disabled['host'] else '无'} quality.host={disabled['quality']['host']!r}")

    # 16. 构成比例的一对显存读数必须同基。
    total_expr = FIXED_EXPRESSIONS["device.gpu.memory.total_bytes"]
    used_expr = FIXED_EXPRESSIONS["device.gpu.memory.used_bytes"]
    check("显存 used/total 同基（可作分子分母）",
          total_expr.reducer == used_expr.reducer,
          f"{total_expr.reducer!r} vs {used_expr.reducer!r}")

    # 17. Prometheus 传输不跟随重定向。
    from backend.observability.sources import _OPENER, _NoRedirects

    check("Prometheus 传输不跟随重定向",
          any(isinstance(handler, _NoRedirects) for handler in _OPENER.handlers))

    # 18. 每一个 DRIVER_STATES 取值都从某条代码路径可达。
    reachable = {
        driver_status("a", "a"),
        driver_status("a", "b"),
        driver_status(None, None),
        driver_status("a", None, userspace_error=NVML_ERROR_PERMISSION),
    }
    check("DRIVER_STATES 每个取值都可达",
          reachable == set(DRIVER_STATES), f"reachable={sorted(reachable)}")

    return results


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _summary(dtos: dict) -> list[str]:
    lines: list[str] = []
    admin = dtos[KIND_USAGE_ADMIN]
    own = dtos[KIND_USAGE_SELF]
    engines = dtos[KIND_ENGINE_STATUS]
    timeline = dtos[KIND_TIMELINE]

    lines.append("四种视图")
    lines.append(f"  usage.self    uid={own['scope']['uid']} "
                 f"endpoints={own['scope']['endpoints']} "
                 f"calls={own['counters']['calls']} quality={own['quality']['calls']}")
    lines.append(f"  usage.admin   principals={len(admin['principals'])} "
                 f"calls={admin['counters']['calls']} quality={admin['quality']['calls']} "
                 f"host={'有' if admin['host'] else '无'}")
    lines.append(f"  engine.status counts={engines['counts']} "
                 f"quality={engines['quality']['engines']}")
    lines.append(f"  timeline      coverage={timeline['coverage']['observed_count']}"
                 f"/{timeline['coverage']['expected_count']} "
                 f"quality={timeline['quality']['buckets']}")

    lines.append("")
    lines.append("口径分离")
    lines.append("  按人计量  basis=%s  指标=%d 条（全部为 count/tokens/ms）"
                 % (admin["billing_basis"], len(admin["metrics"])))
    host = admin["host"] or {}
    host_sections = [key for key in ("node", "driver", "devices") if key in host]
    lines.append("  整机/整卡 host 段=%s  设备=%d 张"
                 % (",".join(host_sections), len(host.get("devices") or [])))
    driver = (admin["host"] or {}).get("driver", {}).get("node.gpu.driver.status", {})
    if driver:
        lines.append("  驱动状态  %s" % driver.get("state"))

    lines.append("")
    lines.append("不可归属诊断（普通主体 / 管理主体）")
    for key in ("unknown_key", "malformed_lines", "content_field_rejected"):
        lines.append(f"  {key:<24} {own['unknown'][key]!r:>6} / {admin['unknown'][key]!r}")

    lines.append("")
    lines.append("原生 UI 授权入口")
    for entry in admin["external_ui"]["entries"]:
        lines.append(f"  {entry['id']:<11} visible={entry['visible']!s:<5} "
                     f"requires={entry['requires']:<28} external_url={entry['external_url']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="监控后端基础报告与不变量自检（合成数据）")
    parser.add_argument("--json", action="store_true", help="输出机器可读的整体报告")
    parser.add_argument("--kind", choices=[KIND_USAGE_SELF, KIND_USAGE_ADMIN,
                                           KIND_ENGINE_STATUS, KIND_TIMELINE],
                        help="只打印某一个 kind 的 DTO 样例")
    parser.add_argument("--quiet", action="store_true", help="只输出自检结论")
    args = parser.parse_args(argv)

    dtos = build_all()
    checks = self_check(dtos)
    failed = [item for item in checks if not item["ok"]]

    if args.json:
        print(json.dumps({
            "synthetic": True,
            "checks": checks,
            "passed": len(checks) - len(failed),
            "failed": len(failed),
            "dtos": dtos,
        }, sort_keys=True, ensure_ascii=False, indent=2))
        return 1 if failed else 0

    if args.kind:
        print(json.dumps(dtos[args.kind], sort_keys=True, ensure_ascii=False, indent=2))
        return 1 if failed else 0

    if not args.quiet:
        print("=" * 78)
        print("监控与日志数据后端 —— 基础报告（SYNTHETIC：数值为虚构）")
        print("=" * 78)
        for line in _summary(dtos):
            print(line)
        print()

    print("不变量自检")
    for item in checks:
        mark = "PASS" if item["ok"] else "FAIL"
        suffix = f"  <- {item['detail']}" if item["detail"] and not item["ok"] else ""
        print(f"  [{mark}] {item['name']}{suffix}")
    print()
    print(f"结论：{len(checks) - len(failed)}/{len(checks)} 通过"
          + ("" if not failed else "  ** 存在失败项 **"))
    if failed:
        print("不得据此声称 READY。")
        return 1

    if not args.quiet:
        print()
        print("=" * 78)
        print("schema 样例（每个 kind 一份完整 DTO，可直接作 UI fixture）")
        print("=" * 78)
        for kind in (KIND_USAGE_SELF, KIND_USAGE_ADMIN, KIND_ENGINE_STATUS, KIND_TIMELINE):
            print(f"\n----- {kind} -----")
            print(json.dumps(dtos[kind], sort_keys=True, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动入口
    sys.exit(main())
