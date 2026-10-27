"""Read metrics from one authorized registry service, never a browser-supplied URL."""
from __future__ import annotations

import http.client
import ipaddress
import json
import math
import re
import time

from .engine_live import _iso, _unreachable, project
from ..registry.presence import service_state

MAX_BYTES = 1024 * 1024
SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^\n]*\})?\s+([^\s]+)(?:\s+[^\s]+)?$')


def fetch(engine, path='/metrics', *, key=None, timeout=2.0):
    target = engine.target or {}
    host = target.get('host')
    if host != 'localhost' and not ipaddress.ip_address(host).is_loopback:
        raise ValueError('only loopback services are supported')
    cls = http.client.HTTPSConnection if target.get('scheme') == 'https' else http.client.HTTPConnection
    conn = cls(host, int(target['port']), timeout=timeout)
    headers = {'Accept': 'application/json, text/plain'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    try:
        conn.request('GET', path, headers=headers)
        response = conn.getresponse()
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('response_too_large')
        return response.status, body.decode('utf-8')
    finally:
        conn.close()


def prometheus(payload, *, framework, observed_at):
    values = {}
    for line in payload.splitlines():
        match = SAMPLE.fullmatch(line.strip())
        if not match:
            continue
        try:
            value = float(match[2])
        except ValueError:
            continue
        if math.isfinite(value) and value >= 0:
            values[match[1]] = values.get(match[1], 0) + value
    def first(*names):
        return next((values[n] for n in names if n in values), None)
    prefix = 'vllm' if framework == 'vllm' else 'sglang'
    if not any(k.startswith(prefix + ':') for k in values):
        return _unreachable('metrics_not_enabled', observed_at)
    result = _unreachable(None, observed_at)
    result.update(reachable=True, reason=None, source=framework + '-metrics',
                  engine={}, live={
                      'running': first(prefix + ':num_requests_running', prefix + ':num_running_reqs'),
                      'queued': first(prefix + ':num_requests_waiting', prefix + ':num_queue_reqs'),
                      'slots': [],
                  }, throughput={'tok_s': first('sglang:gen_throughput') if framework == 'sglang' else None}, totals={
                      'requests': first(prefix + ':request_success_total', prefix + ':num_requests_total'),
                      'prompt_tokens': first(prefix + ':prompt_tokens_total'),
                      'output_tokens': first(prefix + ':generation_tokens_total'),
                  }, recent=[])
    return result


def read(engine, *, key=None, getter=fetch):
    at = _iso(time.time())
    framework = (engine.launch or {}).get('framework') or next(
        (t.split(':', 1)[1] for t in engine.tags if t.startswith('framework:')), None)
    if service_state(engine) in ('offline', 'stale'):
        return _unreachable('service_' + service_state(engine), at)
    if framework not in ('strata', 'vllm', 'sglang'):
        return _unreachable('metrics_not_supported', at)
    try:
        status, payload = getter(engine, key=key)
        if status in (401, 403):
            return _unreachable('unauthorized', at)
        if status != 200:
            return _unreachable('metrics_not_enabled' if status == 404 else 'http_error', at)
        if framework == 'strata':
            data = json.loads(payload)
            if not isinstance(data, dict) or not any(isinstance(data.get(k), dict) for k in ('engine', 'live', 'totals')):
                return _unreachable('bad_payload', at)
            return project(data, observed_at=at)
        return prometheus(payload, framework=framework, observed_at=at)
    except (OSError, http.client.HTTPException):
        return _unreachable('unreachable', at)
    except (ValueError, TypeError):
        return _unreachable('bad_payload', at)
