"""Personal management contracts, without contacting production/GPU services."""
from contextlib import closing
from dataclasses import replace
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.bff.personal_benchmarks import EvalScopeClient, numeric_metrics
from backend.bff.service_metrics import prometheus, read
from backend.registry.models import ENGINE_RUNTIME_OFFLINE
from backend.registry.ownership import OwnershipProof, OWNERSHIP_RUNTIME_OBSERVED
from tests.bff import test_gateway as fixtures


class Runner:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def probe(self):
        return {'connected': True, 'perf': True, 'eval': True}

    def run(self, kind, body, *, task_id):
        self.calls.append((kind, body))
        if self.fail:
            raise TimeoutError()
        return {'latency.mean': 0.25, 'requests': 10}

    def stop(self, kind, task_id):
        self.calls.append(('stop', task_id))
        return True


class PersonalApiTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.GatewayTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.app.service.config = replace(self.h.app.service.config, personal_mode=True)

    def test_legacy_shared_routes_cannot_be_used_by_other_members_or_admin(self):
        h = self.h
        route = h.publish()
        self.assertEqual(route['visibility'], 'private')
        # Old database records do not re-enable sharing in personal mode.
        h.app.gateway.store.update_route(route['route_id'], visibility='all')
        for web in (h.member, h.admin):
            self.assertEqual(web.run('GET', '/api/gateway/models').json()['data']['routes'], [])
            key = h.key(web)['key']
            self.assertEqual(h.v1(key, payload={'model': 'qwen', 'messages': []}).status, 401 if web is h.admin else 404)
            self.assertEqual(h.post(web, '/api/gateway/models/publish', {'engine_id': h.engine_id, 'model': 'stolen'}).status, 403)
        self.assertEqual(h.post(h.owner, '/api/gateway/models/publish', {'engine_id': h.engine_id, 'model': 'another', 'visibility': 'all'}).status, 400)

    def test_admin_cannot_change_other_service_or_read_its_credentials_and_prompt(self):
        h = self.h
        h.publish()
        key = h.key(h.owner, log_bodies=True)
        self.assertEqual(h.v1(key['key'], payload={'model': 'qwen', 'messages': [{'role': 'user', 'content': 'private prompt'}]}).status, 200)
        row = h.owner.run('GET', '/api/gateway/logs').json()['data']['rows'][0]
        detail = h.admin.run('GET', '/api/gateway/logs/' + row['request_id']).json()['data']
        self.assertIsNone(detail['body'])
        self.assertEqual(detail['body_access'], 'metadata_only')
        self.assertEqual(h.post(h.admin, f'/api/v2/registry/engines/{h.engine_id}/params/update', {'values': {'parallel': 2}}).status, 403)
        self.assertEqual(h.admin.run('GET', '/api/model-service/key').status, 403)

    def test_metrics_select_an_authorized_engine_and_offline_prevents_inference(self):
        h = self.h
        endpoint = f'/api/v2/registry/engines/{h.engine_id}/live'
        with patch('backend.bff.service_metrics.read', return_value={'reachable': False}) as getter:
            self.assertEqual(h.member.run('GET', endpoint).status, 404)
            getter.assert_not_called()
            self.assertEqual(h.owner.run('GET', endpoint).status, 200)
            self.assertEqual(getter.call_args[0][0].id, h.engine_id)
            self.assertEqual(getter.call_args.kwargs['key'], fixtures.UPSTREAM_SECRET)
        h.publish()
        h.app.service.store.update_engine_runtime(h.engine_id, state=ENGINE_RUNTIME_OFFLINE, last_seen_at=None)
        key = h.key(h.owner)['key']
        self.assertEqual(h.v1(key, payload={'model': 'qwen', 'messages': []}).status, 503)
        self.assertEqual(fixtures.FakeEngine.seen, [])

    def test_unbound_account_cannot_claim_an_unregistered_listener_owned_by_someone_else(self):
        h = self.h
        h.app.service.config = replace(h.app.service.config, allowed_ports=frozenset({9876}))
        h.app.service.ownership_prover = SimpleNamespace(observe=lambda target: OwnershipProof(
            state=OWNERSHIP_RUNTIME_OBSERVED, method='test', uid=4003, linux_user='vince'))
        response = h.post(h.owner, '/api/v2/registry/engines', {
            'name': 'cannot-claim', 'protocol': 'openai_compatible', 'target_url': 'http://127.0.0.1:9876'})
        self.assertNotEqual(response.status, 201)
        self.assertEqual(response.json()['error']['code'], 'registry.ownership_unverified')

    def wait_job(self, job_id):
        h = self.h
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with closing(h.app.benchmarks._db()) as db:
                row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row['status'] != 'running':
                    return h.app.benchmarks.row(row)
            time.sleep(.01)
        self.fail('test runner did not finish')

    def test_benchmark_is_explicit_owner_scoped_idempotent_and_does_not_store_secrets(self):
        h = self.h
        h.publish()
        runner = h.app.benchmarks.client = Runner()
        path = '/api/evaluation/benchmarks'
        self.assertEqual(h.owner.run('GET', path).status, 200)
        self.assertEqual(runner.calls, [])
        body = {'model': 'qwen', 'idempotency_key': 'request_0001', 'number': 10}
        self.assertEqual(h.post(h.member, path + '/submit', body).status, 404)
        self.assertEqual(h.owner.run('POST', path + '/submit', payload=body).status, 403)
        result = h.post(h.owner, path + '/submit', body)
        self.assertEqual(result.status, 202, result.body)
        job_id = result.json()['data']['run']['id']
        job = self.wait_job(job_id)
        self.assertEqual(job['status'], 'completed')
        self.assertFalse(job['synthetic'])
        self.assertEqual(h.post(h.owner, path + '/submit', body).json()['data']['run']['id'], job_id)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]['model'], 'qwen-upstream')
        self.assertEqual(runner.calls[0][1]['api_key'], fixtures.UPSTREAM_SECRET)
        self.assertEqual(h.member.run('GET', path).json()['data']['runs'], [])
        self.assertEqual(h.admin.run('GET', path).json()['data']['runs'], [])
        self.assertNotIn(fixtures.UPSTREAM_SECRET.encode(), h.app.benchmarks.path.read_bytes())
        self.assertEqual(h.post(h.owner, path + '/submit', {**body, 'number': 11}).status, 409)
        for key, value in [('number', True), ('parallel', 5), ('max_tokens', 2000), ('url', 'http://example.test')]:
            self.assertEqual(h.post(h.owner, path + '/submit', {**body, key: value}).status, 400)

    def test_uncertain_benchmark_blocks_retries_until_owner_confirms_runner_stopped(self):
        h = self.h
        h.publish()
        h.app.benchmarks.client = Runner(fail=True)
        path = '/api/evaluation/benchmarks'
        result = h.post(h.owner, path + '/submit', {'model': 'qwen', 'idempotency_key': 'timeout_0001'})
        job_id = result.json()['data']['run']['id']
        self.assertEqual(self.wait_job(job_id)['status'], 'unknown')
        self.assertEqual(h.post(h.owner, path + '/submit', {'model': 'qwen', 'idempotency_key': 'timeout_0002'}).status, 409)
        self.assertEqual(h.post(h.owner, path + '/reconcile', {'id': job_id}).status, 400)
        self.assertEqual(h.post(h.member, path + '/reconcile', {'id': job_id, 'confirmed_stopped': True}).status, 404)
        response = h.post(h.owner, path + '/reconcile', {'id': job_id, 'confirmed_stopped': True})
        self.assertEqual(response.json()['data']['run']['status'], 'reconciled')

    def test_stop_only_targets_the_owners_recorded_runner_task(self):
        h = self.h
        h.publish()
        runner = h.app.benchmarks.client = Runner(fail=True)
        path = '/api/evaluation/benchmarks'
        result = h.post(h.owner, path + '/submit', {'model': 'qwen', 'idempotency_key': 'stop_0001'})
        job_id = result.json()['data']['run']['id']
        self.wait_job(job_id)
        self.assertEqual(h.post(h.member, path + '/stop', {'id': job_id}).status, 404)
        self.assertEqual(h.post(h.owner, path + '/stop', {'id': []}).status, 400)
        response = h.post(h.owner, path + '/stop', {'id': job_id})
        self.assertEqual(response.json()['data']['run']['status'], 'stopped')
        self.assertEqual(runner.calls[-1], ('stop', job_id))


class AdapterTests(unittest.TestCase):
    def test_current_invoke_protocol_carries_task_identity_and_parses_completed_results(self):
        client = EvalScopeClient()
        calls = []
        def request(method, path, payload=None, timeout=2, task_id=None):
            calls.append((method, path, task_id))
            if method == 'GET':
                return 200, '', b'{"service":"evalscope","status":"ok"}'
            if method == 'OPTIONS':
                return 200, 'POST, GET, HEAD, OPTIONS' if path.endswith('/invoke') else 'GET, HEAD, OPTIONS', b''
            return 200, '', json.dumps({'status': 'completed', 'task_id': task_id, 'result': {'gsm8k': [{'score': .8, 'prompt': 'not stored'}]}}).encode()
        client.request = request
        self.assertEqual(client.probe()['protocols'], {'perf': 'invoke', 'eval': 'invoke'})
        self.assertEqual(client.run('eval', {'model': 'm'}, task_id='bench_test'), {'gsm8k.0.score': .8})
        self.assertEqual(calls[-1], ('POST', '/api/v1/eval/invoke', 'bench_test'))

    def test_legacy_execution_endpoint_remains_supported(self):
        client = EvalScopeClient()
        def request(method, path, payload=None, timeout=2, task_id=None):
            if method == 'GET':
                return 200, '', b'{"service":"evalscope","status":"ok"}'
            if method == 'OPTIONS':
                return 200, 'GET, HEAD, OPTIONS' if path.endswith('/invoke') else 'POST, OPTIONS', b''
            self.assertIsNone(task_id)
            return 200, '', b'{"status":"success","results":{"latency":0.5}}'
        client.request = request
        self.assertEqual(client.probe()['protocols']['perf'], 'legacy')
        self.assertEqual(client.run('perf', {}, task_id='bench_old'), {'latency': .5})

    def test_dashboard_health_does_not_claim_execution_capability(self):
        for allow, ready in [('GET, HEAD, OPTIONS', False), ('POST, OPTIONS', True)]:
            client = EvalScopeClient()
            client.request = lambda method, path, a=allow: (200, a, b'{"service":"evalscope","status":"ok"}')
            result = client.probe()
            self.assertTrue(result['connected'])
            self.assertEqual(result['perf'], ready)
        self.assertEqual(numeric_metrics({'score': .8, 'nested': {'latency': 4}, 'prompt': 'secret', 'nan': float('nan'), 'output_dir': '/secret'}), {'score': .8, 'nested.latency': 4})

    def test_prometheus_metrics_preserve_missing_values_and_aggregate_labels(self):
        result = prometheus('vllm:num_requests_running{engine="a"} 2\nvllm:num_requests_running{engine="b"} 3\nvllm:generation_tokens_total NaN', framework='vllm', observed_at='now')
        self.assertEqual(result['live']['running'], 5)
        self.assertIsNone(result['live']['queued'])
        self.assertIsNone(result['totals']['output_tokens'])
        self.assertFalse(prometheus('<html>login</html>', framework='vllm', observed_at='now')['reachable'])
        sg = prometheus('sglang:num_running_reqs 1\nsglang:num_queue_reqs 0\nsglang:gen_throughput 2.5', framework='sglang', observed_at='now')
        self.assertEqual(sg['throughput']['tok_s'], 2.5)
        self.assertEqual(sg['live']['queued'], 0)
