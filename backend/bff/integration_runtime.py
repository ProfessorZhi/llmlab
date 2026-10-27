"""Small, read-only capability checks for the six integrations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import threading
import time
from ..registry.presence import service_state


class IntegrationRuntime:
    def __init__(self, registry, evalscope):
        self.registry, self.evalscope = registry, evalscope
        self._cached, self._at, self._lock = {}, 0, threading.Lock()

    @staticmethod
    def _probe(kind):
        port, path = (9090, '/api/v1/query?query=up') if kind == 'prometheus' else (3000, '/api/health')
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        try:
            conn.request('GET', path, headers={'Accept': 'application/json'})
            response = conn.getresponse()
            raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError('response_too_large')
            data = json.loads(raw)
            connected = response.status == 200 and (data.get('status') == 'success' if kind == 'prometheus' else data.get('database') == 'ok')
            result = {'connected': connected, 'reason': None if connected else 'probe_failed'}
            if connected and kind == 'prometheus':
                targets = (data.get('data') or {}).get('result') or []
                result['targets'] = len(targets)
                result['targets_up'] = sum(1 for row in targets if (row.get('value') or [None, None])[-1] == '1')
            if connected and kind == 'grafana':
                version = data.get('version')
                result['version'] = version[:40] if isinstance(version, str) else None
            return result
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            return {'connected': False, 'reason': 'unreachable'}
        finally:
            conn.close()

    def __call__(self, principal):
        with self._lock:
            if time.monotonic() - self._at > 15 or not self._cached:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    prom = pool.submit(self._probe, 'prometheus')
                    grafana = pool.submit(self._probe, 'grafana')
                    evaluation = pool.submit(self.evalscope.probe)
                    self._cached = {'prometheus': prom.result(), 'grafana': grafana.result(), 'evalscope': evaluation.result()}
                self._at = time.monotonic()
            result = {key: dict(value) for key, value in self._cached.items()}
        engines = [e for e in self.registry.store.list_engines() if e.owner_principal == principal.principal]
        for kind in ('strata', 'vllm', 'sglang'):
            rows = [e for e in engines if (e.launch or {}).get('framework') == kind or 'framework:' + kind in e.tags]
            result[kind] = {'services': len(rows), 'online': sum(service_state(e) not in ('offline', 'stale') and e.status == 'verified' for e in rows),
                            'connected': None, 'reason': 'per_service_metrics',
                            'observed_at': max((e.last_seen_at.isoformat() for e in rows if e.last_seen_at), default=None)}
        return result
