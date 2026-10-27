"""Owner-scoped, real EvalScope HTTP jobs for the management console.

Protocol: https://evalscope.readthedocs.io/en/latest/user_guides/service.html
Only fixed evaluation presets and registered local services cross this boundary.
No prompts, output paths, credentials or raw runner errors are stored in the job ledger.
"""
from __future__ import annotations

from contextlib import closing
import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlsplit

from ..gateway.service import ManagementError
from ..registry.presence import service_state


class RunnerStopped(Exception):
    pass


class EvalScopeClient:
    def __init__(self, base='http://127.0.0.1:9000'):
        parts = urlsplit(base)
        if parts.scheme != 'http' or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
            raise ValueError('EvalScope must be a plain loopback HTTP origin')
        if parts.hostname != 'localhost' and not ipaddress.ip_address(parts.hostname).is_loopback:
            raise ValueError('EvalScope must run on loopback')
        self.host, self.port = parts.hostname, parts.port or 80
        self._cached = None
        self._checked = 0

    def request(self, method, path, payload=None, timeout=2, task_id=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            body = None if payload is None else json.dumps(payload).encode()
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
            if task_id:
                headers['EvalScope-Task-Id'] = task_id
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('runner_response_too_large')
            return response.status, response.getheader('Allow', ''), raw
        finally:
            conn.close()

    def probe(self):
        if self._cached is not None and time.monotonic() - self._checked < 15:
            return dict(self._cached)
        result = {'connected': False, 'perf': False, 'eval': False, 'protocols': {}, 'reason': 'unreachable'}
        try:
            status, _, raw = self.request('GET', '/health')
            health = json.loads(raw)
            if status == 200 and health.get('service') == 'evalscope' and health.get('status') == 'ok':
                result.update(connected=True, reason='execution_api_unavailable')
                for kind in ('perf', 'eval'):
                    # EvalScope Dashboard 1.12 uses /invoke and a task-id header.
                    # Older service releases use the bare /perf and /eval paths.
                    for protocol, suffix in (('invoke', '/invoke'), ('legacy', '')):
                        code, allow, _ = self.request('OPTIONS', '/api/v1/' + kind + suffix)
                        if (200 <= code < 300 or code == 405) and 'POST' in [s.strip() for s in allow.split(',')]:
                            result[kind] = True
                            result['protocols'][kind] = protocol
                            break
                if result['perf'] or result['eval']:
                    result['reason'] = None
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            pass
        self._cached, self._checked = result, time.monotonic()
        return dict(result)

    def run(self, kind, payload, *, task_id):
        protocol = self.probe().get('protocols', {}).get(kind)
        if protocol is None:
            raise ValueError('runner_unavailable')
        suffix = '/invoke' if protocol == 'invoke' else ''
        status, _, raw = self.request('POST', '/api/v1/' + kind + suffix, payload, timeout=300,
                                     task_id=task_id if protocol == 'invoke' else None)
        if status != 200:
            raise ValueError('runner_http_error')
        result = json.loads(raw)
        if result.get('status') == 'stopped' and result.get('task_id') == task_id:
            raise RunnerStopped()
        if result.get('status') not in ('success', 'completed'):
            raise ValueError('runner_failed')
        if protocol == 'invoke' and result.get('task_id') != task_id:
            raise ValueError('runner_task_mismatch')
        return numeric_metrics(result.get('results') if 'results' in result else result.get('result'))

    def stop(self, kind, task_id):
        if self.probe().get('protocols', {}).get(kind) != 'invoke':
            return False
        status, _, raw = self.request('POST', '/api/v1/' + kind + '/stop?task_id=' + task_id, timeout=10)
        result = json.loads(raw)
        return status == 200 and result.get('status') == 'stopped' and result.get('task_id') == task_id


def numeric_metrics(value, prefix='', depth=0):
    """Keep only bounded numeric summaries, not response text or filesystem paths."""
    out = {}
    if depth > 5 or not isinstance(value, dict):
        return out
    for key, item in list(value.items())[:100]:
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_ .%()/-]{1,80}', key):
            continue
        name = f'{prefix}.{key}' if prefix else key
        if type(item) in (float, int) and math.isfinite(item):
            out[name] = item
        elif isinstance(item, dict):
            out.update(numeric_metrics(item, name, depth + 1))
        elif isinstance(item, list):
            for index, child in enumerate(item[:100]):
                if isinstance(child, dict):
                    out.update(numeric_metrics(child, f'{name}.{index}', depth + 1))
                if len(out) >= 200:
                    break
        if len(out) >= 200:
            break
    return dict(list(out.items())[:200])


class PersonalBenchmarks:
    def __init__(self, path, gateway, client=None):
        self.path = Path(path)
        self.gateway = gateway
        self.client = client or EvalScopeClient()
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, '
                       'request_key TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, '
                       'status TEXT NOT NULL, spec TEXT NOT NULL, result TEXT, reason TEXT, UNIQUE(owner,request_key))')
            db.execute("UPDATE jobs SET status='unknown', reason='platform_restarted' WHERE status='running'")
        self.path.chmod(0o600)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def row(row):
        return {'id': row['id'], 'created_at': row['created'], 'updated_at': row['updated'],
                'status': row['status'], 'spec': json.loads(row['spec']),
                'metrics': json.loads(row['result'] or '{}'), 'reason': row['reason'],
                'source': 'evalscope', 'synthetic': False}

    def listing(self, principal):
        with closing(self._db()) as db:
            rows = db.execute('SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 100',
                              (principal.principal,)).fetchall()
        return {'runs': [self.row(r) for r in rows], 'runner': self.client.probe()}

    def reconcile(self, principal, body):
        if body.get('confirmed_stopped') is not True or not isinstance(body.get('id'), str):
            raise ManagementError(400, 'evaluation.confirmation_required', 'confirm the runner has finished before releasing the slot')
        with self.lock, closing(self._db()) as db, db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND owner=?', (body['id'], principal.principal)).fetchone()
            if row is None:
                raise ManagementError(404, 'evaluation.not_found', 'no such owned benchmark')
            if row['status'] != 'unknown':
                raise ManagementError(409, 'evaluation.conflict', 'only an uncertain run can be reconciled')
            db.execute("UPDATE jobs SET status='reconciled',reason='owner_confirmed_stopped',updated=? WHERE id=?", (time.time(), body['id']))
            return self.row(db.execute('SELECT * FROM jobs WHERE id=?', (body['id'],)).fetchone())

    def stop(self, principal, body):
        if not isinstance(body.get('id'), str):
            raise ManagementError(400, 'evaluation.invalid_request', 'benchmark id is required')
        with closing(self._db()) as db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND owner=?', (body.get('id'), principal.principal)).fetchone()
        if row is None:
            raise ManagementError(404, 'evaluation.not_found', 'no such owned benchmark')
        if row['status'] not in ('running', 'unknown'):
            raise ManagementError(409, 'evaluation.conflict', 'benchmark is already finished')
        try:
            stopped = self.client.stop(json.loads(row['spec'])['kind'], row['id'])
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            stopped = False
        if not stopped:
            raise ManagementError(409, 'evaluation.stop_unconfirmed', 'EvalScope did not confirm the task was stopped')
        with closing(self._db()) as db, db:
            db.execute("UPDATE jobs SET status='stopped', reason=NULL, updated=? WHERE id=? AND status IN ('running','unknown')", (time.time(), row['id']))
            return self.row(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())

    def submit(self, principal, body):
        allowed = {'model', 'kind', 'number', 'parallel', 'max_tokens', 'dataset', 'idempotency_key'}
        if set(body) - allowed:
            raise ManagementError(400, 'evaluation.invalid_request', 'unsupported benchmark fields')
        model = body.get('model')
        route = self.gateway.store.route_by_alias(model) if isinstance(model, str) else None
        if route is None or route.owner != principal.principal:
            raise ManagementError(404, 'evaluation.not_found', 'no such owned API service')
        engine = self.gateway.core.engine(route.engine_id)
        if engine is None or not route.enabled or service_state(engine) in ('offline', 'stale') or engine.status == 'unavailable':
            raise ManagementError(409, 'evaluation.service_offline', 'start the service before benchmarking')
        kind = body.get('kind', 'perf')
        if kind not in ('perf', 'eval'):
            raise ManagementError(400, 'evaluation.invalid_request', 'kind must be perf or eval')
        def integer(name, default, maximum):
            value = body.get(name, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ManagementError(400, 'evaluation.invalid_request', f'{name} must be between 1 and {maximum}')
            return value
        number, parallel, tokens = integer('number', 10, 100), integer('parallel', 1, 4), integer('max_tokens', 128, 1024)
        dataset = body.get('dataset', 'openqa' if kind == 'perf' else 'gsm8k')
        if dataset not in (('openqa',) if kind == 'perf' else ('gsm8k',)):
            raise ManagementError(400, 'evaluation.invalid_request', 'unsupported dataset preset')
        request_key = body.get('idempotency_key')
        if not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_key):
            raise ManagementError(400, 'evaluation.invalid_request', 'idempotency_key is required')
        spec = {'model': model, 'engine_id': engine.id, 'kind': kind, 'number': number,
                'parallel': min(parallel, number), 'max_tokens': tokens, 'dataset': dataset}
        encoded = json.dumps(spec, sort_keys=True)
        with self.lock, closing(self._db()) as db, db:
            previous = db.execute('SELECT * FROM jobs WHERE owner=? AND request_key=?', (principal.principal, request_key)).fetchone()
            if previous:
                if previous['spec'] != encoded:
                    raise ManagementError(409, 'evaluation.conflict', 'idempotency key already used with different parameters')
                return self.row(previous)
            if not self.client.probe().get(kind):
                raise ManagementError(503, 'evaluation.runner_unavailable', 'EvalScope execution API is unavailable')
            active = db.execute("SELECT owner FROM jobs WHERE status IN ('running','unknown')").fetchall()
            if len(active) >= 2 or any(r['owner'] == principal.principal for r in active):
                raise ManagementError(409, 'evaluation.busy', 'a benchmark is active or its runner status needs reconciliation')
            job_id, now = 'bench_' + uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)',
                       (job_id, principal.principal, request_key, now, now, 'running', encoded, None, None))
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        # The user has explicitly started this job. Rendering a page never starts inference.
        threading.Thread(target=self._run, args=(job_id, principal.principal, route.id, spec), daemon=True).start()
        return self.row(row)

    def _run(self, job_id, owner, route_id, spec):
        status, reason, metrics = 'failed', 'runner_failed', {}
        try:
            route = self.gateway.store.route_by_id(route_id)
            if route is None or route.owner != owner or not route.enabled or not self.gateway.core.principal_active(owner):
                raise ValueError('service_changed')
            engine = self.gateway.core.engine(route.engine_id)
            if engine is None or service_state(engine) in ('offline', 'stale') or engine.status == 'unavailable':
                raise ValueError('service_offline')
            target = engine.target
            host = target.get('host')
            if host != 'localhost' and not ipaddress.ip_address(host).is_loopback:
                raise ValueError('nonlocal_service')
            host = f'[{host}]' if ':' in host else host
            base = f"{target.get('scheme', 'http')}://{host}:{int(target['port'])}/v1"
            payload = {'model': route.upstream_model or route.alias,
                       'api_key': self.gateway.core.upstream_key(engine) or 'EMPTY'}
            if spec['kind'] == 'perf':
                payload.update(url=base + '/chat/completions', api='openai', number=spec['number'],
                               parallel=spec['parallel'], max_tokens=spec['max_tokens'],
                               dataset=spec['dataset'], stream=True, temperature=0.0)
            else:
                payload.update(api_url=base, datasets=[spec['dataset']], limit=spec['number'],
                               eval_batch_size=spec['parallel'],
                               generation_config={'temperature': 0.0, 'max_tokens': spec['max_tokens']})
            metrics = self.client.run(spec['kind'], payload, task_id=job_id)
            status, reason = 'completed', None if metrics else 'numeric_summary_unavailable'
        except RunnerStopped:
            status, reason = 'stopped', None
        except (TimeoutError, OSError, http.client.HTTPException):
            # A disconnect does not prove that the remote task stopped.
            status, reason = 'unknown', 'runner_completion_unknown'
        except (ValueError, TypeError, KeyError):
            pass
        finally:
            with closing(self._db()) as db, db:
                db.execute("UPDATE jobs SET status=?, result=?, reason=?, updated=? WHERE id=? AND status='running'",
                           (status, json.dumps(metrics), reason, time.time(), job_id))
