"""评测任务的轻量 SQLite 持久层。

单机、几人使用，所以这里**没有**连接池、没有 ORM、没有异步：一个连接、一把
:class:`threading.RLock`、显式事务。数据库是本地文件（或内存库），不是网络服务。

三件不能含糊的事
----------------

**迁移不匹配 = 拒绝启动。** 用 ``PRAGMA user_version`` 记录模式版本。版本不是本模块
认识的数字时抛 :class:`~backend.evaluation.errors.StorageError`，而不是"尽力按现状读写"。
一个读错了列语义的评测库，产出的报告比没有报告更糟。

**状态迁移是带条件写入，不是读-改-写。** :meth:`Store.transition_job` 的 UPDATE 语句
自带 ``WHERE status = ?``（期望的当前状态）。两个并发的取消请求里，只有一个能把
``queued`` 改成 ``canceled``；另一个会看到 ``rowcount == 0``，然后按"已经是终态"
处理成**幂等成功**。这样重试与取消天然是幂等的，不需要额外的锁协议。

**审计只记 metadata。** :meth:`Store.record_audit` 对 ``detail`` 的键做白名单校验。
参数值、报告内容、prompt、任何正文类字段都**进不去**审计表——不是靠调用方自觉，
是靠这一层拒绝。审计要回答的是"谁在什么时候对哪个任务做了什么"，不是"内容是什么"。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping

from .datasets import EvalSpec
from .errors import Conflict, InvalidRequest, StorageError
from .models import (
    ALLOWED_METRIC_NAMES,
    METRIC_STATES,
    Clock,
    EvalJob,
    EvalResult,
    EvalTarget,
    MetricSample,
    isoformat,
    parse_timestamp,
    utc_now,
)

#: 当前模式版本。任何破坏兼容的列变更都要 +1，并让旧库拒绝启动。
SCHEMA_VERSION = 1

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

#: 审计 ``detail`` 允许出现的键。白名单而不是黑名单：新加一个键必须是有意的。
AUDIT_ALLOWED_DETAIL_KEYS = frozenset(
    {
        "eval_type",
        "dataset_slug",
        "dataset_version",
        "preset",
        "params_hash",
        "deployment_id",
        "target_owner",
        "shared",
        "gpu_stress_allowed",
        "from_status",
        "to_status",
        "attempt",
        "retry_of",
        "retry_job_id",
        "idempotency",
        "reason",
        "runner_kind",
        "synthetic",
        "dispatch_state",
        "report_referenced",
        "result_version",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_jobs (
    job_id              TEXT PRIMARY KEY,
    owner_principal     TEXT NOT NULL,
    owner_uid           INTEGER NOT NULL,
    project_id          TEXT NOT NULL,
    idempotency_key     TEXT,
    eval_type           TEXT NOT NULL,
    dataset_slug        TEXT NOT NULL,
    dataset_version     TEXT NOT NULL,
    preset              TEXT,
    params_json         TEXT NOT NULL,
    deployment_id       TEXT NOT NULL,
    target_project_id   TEXT NOT NULL,
    target_owner        TEXT NOT NULL,
    target_engine       TEXT,
    shared              INTEGER NOT NULL,
    gpu_stress_allowed  INTEGER NOT NULL,
    status              TEXT NOT NULL,
    synthetic           INTEGER NOT NULL,
    runner_kind         TEXT NOT NULL,
    dispatch_state      TEXT NOT NULL,
    dispatch_reason     TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    attempt             INTEGER NOT NULL DEFAULT 1,
    retry_of            TEXT,
    failure_code        TEXT,
    failure_detail      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS eval_jobs_idempotency
    ON eval_jobs(owner_principal, project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS eval_jobs_owner ON eval_jobs(owner_principal, created_at);
CREATE INDEX IF NOT EXISTS eval_jobs_project ON eval_jobs(project_id, created_at);
CREATE INDEX IF NOT EXISTS eval_jobs_status ON eval_jobs(status);

CREATE TABLE IF NOT EXISTS eval_results (
    result_id    TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL REFERENCES eval_jobs(job_id),
    version      INTEGER NOT NULL,
    synthetic    INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    report_ref   TEXT,
    summary      TEXT,
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS eval_results_version ON eval_results(job_id, version);
CREATE INDEX IF NOT EXISTS eval_results_job ON eval_results(job_id, version DESC);

CREATE TABLE IF NOT EXISTS eval_rate_events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    key   TEXT NOT NULL,
    at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS eval_rate_lookup ON eval_rate_events(scope, key, at);

CREATE TABLE IF NOT EXISTS eval_audit (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    at               TEXT NOT NULL,
    actor_principal  TEXT NOT NULL,
    actor_uid        INTEGER NOT NULL,
    action           TEXT NOT NULL,
    job_id           TEXT,
    outcome          TEXT NOT NULL,
    reason           TEXT,
    detail_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS eval_audit_job ON eval_audit(job_id, id);
"""


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _loads(raw: str) -> object:
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:  # pragma: no cover - 写路径已保证合法
        raise StorageError(f"stored JSON is unreadable: {exc}") from None


def _require_text(value: object, *, what: str) -> str:
    if not isinstance(value, str):
        raise InvalidRequest(f"{what} must be a string")
    return value


@dataclass(frozen=True)
class AuditEntry:
    id: int
    at: datetime
    actor_principal: str
    actor_uid: int
    action: str
    job_id: str | None
    outcome: str
    reason: str | None
    detail: Mapping[str, object]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "at": isoformat(self.at),
            "actor": self.actor_principal,
            "actor_uid": self.actor_uid,
            "action": self.action,
            "job_id": self.job_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


class Store:
    """评测任务库。单连接 + 一把锁，够用且可预测。"""

    def __init__(self, path: str | Path = ":memory:", *, clock: Clock = utc_now) -> None:
        self._path = str(path)
        self._clock = clock
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open the evaluation store: {exc}") from None
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # -- 迁移 ---------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                # 全新库（或旧式空库）：建表并打上版本号。
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.commit()
                return
            if version != SCHEMA_VERSION:
                raise StorageError(
                    f"evaluation store has schema version {version}, "
                    f"this build understands {SCHEMA_VERSION}; refusing to run"
                )
            # 版本一致也要保证表存在（例如从只读副本恢复）。executescript 幂等。
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - 关闭失败无从补救
                pass

    # -- 任务 ---------------------------------------------------------------

    def create_job(self, job: EvalJob) -> EvalJob:
        """插入一个任务。

        幂等键冲突时**不**盲目报错：先取出已存在的那一条，内容一致就把它原样返回
        （这就是幂等的含义），内容不同才是 :class:`Conflict`——一个键被两种不同的
        请求复用了，那是调用方的错误，必须说出来。
        """
        if not isinstance(job, EvalJob):
            raise InvalidRequest("create_job expects an EvalJob")
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        """
                        INSERT INTO eval_jobs (
                            job_id, owner_principal, owner_uid, project_id, idempotency_key,
                            eval_type, dataset_slug, dataset_version, preset, params_json,
                            deployment_id, target_project_id, target_owner, target_engine,
                            shared, gpu_stress_allowed, status, synthetic, runner_kind,
                            dispatch_state, dispatch_reason, cancel_requested, attempt, retry_of,
                            failure_code, failure_detail, created_at, updated_at,
                            started_at, finished_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        self._job_columns(job),
                    )
            except sqlite3.IntegrityError:
                existing = self._lookup_by_idempotency(
                    job.owner_principal, job.project_id, job.idempotency_key
                )
                if existing is not None:
                    if self._same_request(existing, job):
                        return existing
                    raise Conflict(
                        "idempotency key was already used for a different request"
                    ) from None
                raise Conflict(f"job {job.job_id!r} already exists") from None
            return job

    def _job_columns(self, job: EvalJob) -> tuple:
        return (
            job.job_id,
            job.owner_principal,
            job.owner_uid,
            job.project_id,
            job.idempotency_key,
            job.spec.eval_type,
            job.spec.dataset_slug,
            job.spec.dataset_version,
            job.spec.preset,
            _dumps(dict(job.spec.params)),
            job.target.deployment_id,
            job.target.project_id,
            job.target.owner_principal,
            job.target.engine,
            1 if job.target.shared else 0,
            1 if job.target.gpu_stress_allowed else 0,
            job.status,
            1 if job.synthetic else 0,
            job.runner_kind,
            job.dispatch_state,
            job.dispatch_reason,
            1 if job.cancel_requested else 0,
            job.attempt,
            job.retry_of,
            job.failure_code,
            job.failure_detail,
            isoformat(job.created_at),
            isoformat(job.updated_at),
            isoformat(job.started_at),
            isoformat(job.finished_at),
        )

    def _lookup_by_idempotency(
        self, owner: str, project: str, key: str | None
    ) -> EvalJob | None:
        if key is None:
            return None
        row = self._conn.execute(
            """
            SELECT * FROM eval_jobs
            WHERE owner_principal = ? AND project_id = ? AND idempotency_key = ?
            """,
            (owner, project, key),
        ).fetchone()
        return self._row_to_job(row) if row else None

    @staticmethod
    def _same_request(existing: EvalJob, incoming: EvalJob) -> bool:
        """两次请求是否"同一件事"：同目标 + 同规格。"""
        return (
            existing.spec.to_dict() == incoming.spec.to_dict()
            and existing.target.deployment_id == incoming.target.deployment_id
        )

    def find_by_idempotency(
        self, *, owner_principal: str, project_id: str, idempotency_key: str | None
    ) -> EvalJob | None:
        with self._lock:
            return self._lookup_by_idempotency(owner_principal, project_id, idempotency_key)

    def get_job(self, job_id: object) -> EvalJob | None:
        if not isinstance(job_id, str) or not job_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM eval_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._row_to_job(row) if row else None

    def list_jobs(
        self,
        *,
        owner_principal: str | None = None,
        project_ids: Iterable[str] | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        all_owners: bool = False,
    ) -> list[EvalJob]:
        """列出任务。

        ``all_owners`` 由 ``authz`` 在确认调用者持有全局读权限之后才允许置位；这里
        不自行判断权限，但也不提供"不设 owner 就返回全部"的默认行为——省略
        ``owner_principal`` 只会返回空列表，除非显式 ``all_owners=True``。
        """
        bounded = self._bounded_limit(limit)
        if not all_owners and not owner_principal:
            return []

        clauses: list[str] = []
        args: list[object] = []
        if not all_owners:
            clauses.append("owner_principal = ?")
            args.append(owner_principal)
        if project_ids is not None:
            ids = [str(p) for p in project_ids]
            if not ids:
                return []
            clauses.append(f"project_id IN ({','.join('?' * len(ids))})")
            args.extend(ids)
        if statuses is not None:
            statuses = list(statuses)
            if not statuses:
                return []
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM eval_jobs {where} ORDER BY rowid DESC LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (*args, bounded, max(0, int(offset)))).fetchall()
            return [self._row_to_job(row) for row in rows]

    def count_by_status(
        self,
        *,
        owner_principal: str | None = None,
        project_ids: Iterable[str] | None = None,
        all_owners: bool = False,
    ) -> dict[str, int]:
        """按状态计数。

        **必须**接受与 :meth:`list_jobs` 同一组过滤条件（``all_owners`` /
        ``owner_principal`` / ``project_ids``），否则监控面板里的总数会与它旁边列出的
        任务数对不上——一个已经离开项目的人会看到"3 个任务"却只列出 0 个。计数与列表
        走同一条可见性规则，是这一步的全部意义。
        """
        clauses: list[str] = []
        args: list[object] = []
        if not all_owners:
            clauses.append("owner_principal = ?")
            args.append(owner_principal or "")
            if project_ids is not None:
                ids = [str(item) for item in project_ids]
                if not ids:
                    return {}
                clauses.append(f"project_id IN ({','.join('?' * len(ids))})")
                args.extend(ids)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT status, COUNT(*) AS n FROM eval_jobs {where} GROUP BY status", tuple(args)
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    @staticmethod
    def _bounded_limit(limit: object) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise InvalidRequest("limit must be an integer")
        if limit < 1:
            raise InvalidRequest("limit must be >= 1")
        return min(limit, MAX_LIST_LIMIT)

    def transition_job(
        self,
        job_id: str,
        *,
        expected_status: str,
        new_status: str,
        at: datetime,
        cancel_requested: bool | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
        dispatch_state: str | None = None,
        dispatch_reason: str | None = None,
        synthetic: bool | None = None,
        runner_kind: str | None = None,
    ) -> EvalJob | None:
        """带条件的迁移：只有当前状态**仍然**是 ``expected_status`` 才生效。

        返回迁移后的任务；``rowcount == 0`` 时返回 ``None``（调用方据此判断"被别人
        抢先改了"，并按幂等语义处理）。这不使用读-改-写，因此并发下不会丢更新。
        """
        # 只写"这次调用显式给了"的列：None 意味着"不动这一列"，而不是"把它清空"。
        # 想清空一个可空列（例如 finished_at）要显式传值，不靠省略。
        optional: dict[str, object] = {
            "cancel_requested": None if cancel_requested is None else (1 if cancel_requested else 0),
            "started_at": isoformat(started_at) if started_at is not None else None,
            "finished_at": isoformat(finished_at) if finished_at is not None else None,
            "failure_code": failure_code,
            "failure_detail": failure_detail,
            "dispatch_state": dispatch_state,
            "dispatch_reason": dispatch_reason,
            "synthetic": None if synthetic is None else (1 if synthetic else 0),
            "runner_kind": runner_kind,
        }

        assignments = ["status = ?", "updated_at = ?"]
        args: list[object] = [new_status, isoformat(at)]
        for column, value in optional.items():
            if value is None:
                continue
            assignments.append(f"{column} = ?")
            args.append(value)

        sql = (
            f"UPDATE eval_jobs SET {', '.join(assignments)} "
            "WHERE job_id = ? AND status = ?"
        )
        args.extend([job_id, expected_status])
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(sql, tuple(args))
                if cursor.rowcount == 0:
                    return None
            return self.get_job(job_id)

    def set_cancel_requested(self, job_id: str, *, at: datetime) -> EvalJob | None:
        """只置取消标志位，不改状态。

        "请求取消" 与 "已经取消" 是两件事：前者对运行中的任务是有意义的（runner 需要
        看到它并停下），后者是状态机的事。
        """
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE eval_jobs SET cancel_requested = 1, updated_at = ? WHERE job_id = ?",
                    (isoformat(at), job_id),
                )
            return self.get_job(job_id)

    def _row_to_job(self, row: sqlite3.Row) -> EvalJob:
        params = _loads(row["params_json"])
        if not isinstance(params, dict):
            raise StorageError("stored params are not an object")
        return EvalJob(
            job_id=row["job_id"],
            owner_principal=row["owner_principal"],
            owner_uid=int(row["owner_uid"]),
            project_id=row["project_id"],
            idempotency_key=row["idempotency_key"],
            spec=EvalSpec(
                eval_type=row["eval_type"],
                dataset_slug=row["dataset_slug"],
                dataset_version=row["dataset_version"],
                params=params,
                preset=row["preset"],
            ),
            target=EvalTarget(
                deployment_id=row["deployment_id"],
                project_id=row["target_project_id"],
                owner_principal=row["target_owner"],
                shared=bool(row["shared"]),
                gpu_stress_allowed=bool(row["gpu_stress_allowed"]),
                engine=row["target_engine"],
            ),
            status=row["status"],
            synthetic=bool(row["synthetic"]),
            runner_kind=row["runner_kind"],
            dispatch_state=row["dispatch_state"],
            dispatch_reason=row["dispatch_reason"],
            cancel_requested=bool(row["cancel_requested"]),
            attempt=int(row["attempt"]),
            retry_of=row["retry_of"],
            failure_code=row["failure_code"],
            failure_detail=row["failure_detail"],
            created_at=parse_timestamp(row["created_at"]),
            updated_at=parse_timestamp(row["updated_at"]),
            started_at=parse_timestamp(row["started_at"]),
            finished_at=parse_timestamp(row["finished_at"]),
        )

    # -- 结果 ---------------------------------------------------------------

    def add_result(self, result: EvalResult) -> EvalResult:
        """追加一个结果版本。版本号由库分配：``max(version) + 1``。"""
        if not isinstance(result, EvalResult):
            raise InvalidRequest("add_result expects an EvalResult")
        with self._lock:
            with self._conn:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS v FROM eval_results WHERE job_id = ?",
                    (result.job_id,),
                ).fetchone()
                version = int(row["v"]) + 1
                stored = EvalResult(
                    result_id=result.result_id,
                    job_id=result.job_id,
                    version=version,
                    synthetic=result.synthetic,
                    metrics=result.metrics,
                    report_ref=result.report_ref,
                    summary=result.summary,
                    created_at=result.created_at or self._clock(),
                )
                self._conn.execute(
                    """
                    INSERT INTO eval_results
                        (result_id, job_id, version, synthetic, metrics_json,
                         report_ref, summary, created_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.result_id,
                        stored.job_id,
                        stored.version,
                        1 if stored.synthetic else 0,
                        _dumps({k: v.to_dict() for k, v in stored.metrics.items()}),
                        stored.report_ref,
                        stored.summary,
                        isoformat(stored.created_at),
                    ),
                )
            return stored

    def get_result(self, job_id: str, *, version: int | None = None) -> EvalResult | None:
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    "SELECT * FROM eval_results WHERE job_id = ? ORDER BY version DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM eval_results WHERE job_id = ? AND version = ?",
                    (job_id, version),
                ).fetchone()
            return self._row_to_result(row) if row else None

    def list_results(self, job_id: str, *, limit: int = DEFAULT_LIST_LIMIT) -> list[EvalResult]:
        bounded = self._bounded_limit(limit)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM eval_results WHERE job_id = ? ORDER BY version DESC LIMIT ?",
                (job_id, bounded),
            ).fetchall()
            return [self._row_to_result(row) for row in rows]

    def _row_to_result(self, row: sqlite3.Row) -> EvalResult:
        raw = _loads(row["metrics_json"])
        if not isinstance(raw, dict):
            raise StorageError("stored metrics are not an object")
        metrics = {}
        for name, payload in raw.items():
            if name not in ALLOWED_METRIC_NAMES:
                raise StorageError(f"stored result carries an unsupported metric: {name!r}")
            if not isinstance(payload, dict):
                raise StorageError(f"stored metric {name!r} is not an object")
            state = payload.get("state")
            if state not in METRIC_STATES:
                raise StorageError(f"stored metric {name!r} has an unknown state")
            metrics[name] = MetricSample(
                name=name,
                state=state,
                value=payload.get("value"),
                unit=payload.get("unit"),
                note=payload.get("note"),
            )
        return EvalResult(
            result_id=row["result_id"],
            job_id=row["job_id"],
            version=int(row["version"]),
            synthetic=bool(row["synthetic"]),
            metrics=metrics,
            report_ref=row["report_ref"],
            summary=row["summary"],
            created_at=parse_timestamp(row["created_at"]),
        )

    # -- 配额事件 -----------------------------------------------------------

    def record_rate_event(self, scope: str, key: str, *, at: datetime) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO eval_rate_events (scope, key, at) VALUES (?,?,?)",
                    (_require_text(scope, what="scope"), _require_text(key, what="key"), isoformat(at)),
                )

    def count_rate_events(self, scope: str, key: str, *, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM eval_rate_events WHERE scope = ? AND key = ? AND at >= ?",
                (scope, key, isoformat(since)),
            ).fetchone()
        return int(row["n"])

    def oldest_rate_event_at(self, scope: str, key: str, *, since: datetime) -> datetime | None:
        """窗口里最早一次事件的时刻。用来把 ``Retry-After`` 算准（见 ``limits``）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(at) AS oldest FROM eval_rate_events WHERE scope = ? AND key = ? AND at >= ?",
                (scope, key, isoformat(since)),
            ).fetchone()
        if row is None or row["oldest"] is None:
            return None
        return parse_timestamp(row["oldest"])

    def prune_rate_events(self, *, before: datetime) -> int:
        """删除窗口之外的事件。返回删除行数（供测试与运维核对）。"""
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM eval_rate_events WHERE at < ?", (isoformat(before),)
                )
            return int(cursor.rowcount or 0)

    # -- 审计 ---------------------------------------------------------------

    def record_audit(
        self,
        *,
        actor_principal: str,
        actor_uid: int,
        action: str,
        outcome: str,
        job_id: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, object] | None = None,
        at: datetime | None = None,
    ) -> None:
        """写一条审计。``detail`` 的键必须在白名单里。

        白名单的意义：审计表会长期保留，而**内容类数据不该进审计**。一个"顺手把
        参数原样塞进 detail"的改动会在这里直接抛错，而不是在半年后变成一次数据泄露。
        """
        payload = dict(detail or {})
        offending = sorted(set(payload) - AUDIT_ALLOWED_DETAIL_KEYS)
        if offending:
            raise InvalidRequest(f"audit detail may not carry: {', '.join(offending)}")
        moment = at or self._clock()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO eval_audit
                        (at, actor_principal, actor_uid, action, job_id, outcome, reason, detail_json)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        isoformat(moment),
                        _require_text(actor_principal, what="actor_principal"),
                        int(actor_uid),
                        _require_text(action, what="action"),
                        job_id,
                        _require_text(outcome, what="outcome"),
                        reason,
                        _dumps(payload),
                    ),
                )

    def list_audit(self, *, job_id: str | None = None, limit: int = DEFAULT_LIST_LIMIT) -> list[AuditEntry]:
        bounded = self._bounded_limit(limit)
        with self._lock:
            if job_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM eval_audit ORDER BY id DESC LIMIT ?", (bounded,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM eval_audit WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                    (job_id, bounded),
                ).fetchall()
        entries = []
        for row in rows:
            detail = _loads(row["detail_json"])
            entries.append(
                AuditEntry(
                    id=int(row["id"]),
                    at=parse_timestamp(row["at"]),
                    actor_principal=row["actor_principal"],
                    actor_uid=int(row["actor_uid"]),
                    action=row["action"],
                    job_id=row["job_id"],
                    outcome=row["outcome"],
                    reason=row["reason"],
                    detail=detail if isinstance(detail, dict) else {},
                )
            )
        return entries


def window_start(now: datetime, seconds: int) -> datetime:
    return now - timedelta(seconds=int(seconds))


__all__ = [
    "AUDIT_ALLOWED_DETAIL_KEYS",
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "SCHEMA_VERSION",
    "AuditEntry",
    "Store",
    "window_start",
]
