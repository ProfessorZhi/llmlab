from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from backend.registry.reported_models import adopt_reported_models
from backend.registry.store import Store
from backend.registry.presence import service_state
from backend.registry.discovery.rules import scan_tree
from cli.llmlab.inventory import inventory_fingerprint
from tests.registry import test_agent_reports as fixtures


class ModelIdentityTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)

    def adopt(self, entries, *, full=True, roots=('/home/alice/models',)):
        return adopt_reported_models(self.store, owner=fixtures.ALICE_ACCOUNT, uid=fixtures.ALICE_UID,
                                     models=entries, full=full, scan_roots=roots, now=fixtures.NOW)

    def test_move_copy_and_missing_keep_identity_and_scanned_scope(self):
        original = {'path': '/home/alice/models/a.gguf', 'suggested_name': 'model-a', 'format': 'gguf',
                    'fingerprint': '100:' + 'a' * 64, 'size_bytes': 100, 'device': 1, 'inode': 10}
        model_id = self.adopt([original]).created[0]
        moved = {**original, 'path': '/home/alice/models/moved.gguf'}
        self.assertEqual(self.adopt([moved]).created, ())
        self.assertEqual(self.store.model_by_id(model_id).weight_path, moved['path'])
        self.assertEqual({l.path: l.state for l in self.store.locations_for(model_id)}, {original['path']: 'missing', moved['path']: 'present'})
        copy = {**original, 'path': '/home/alice/models/copy.gguf', 'device': 2, 'inode': 20}
        self.adopt([moved, copy])
        self.assertEqual(len(self.store.list_models()), 1)
        self.assertEqual(sum(l.state == 'present' for l in self.store.locations_for(model_id)), 2)
        self.adopt([], full=False)
        self.adopt([], roots=('/another/directory',))
        self.assertEqual(sum(l.state == 'present' for l in self.store.locations_for(model_id)), 2)
        self.adopt([])
        self.assertTrue(all(l.state == 'missing' for l in self.store.locations_for(model_id)))

    def test_same_size_different_weights_have_different_relocation_fingerprints(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, content in [('first', b'a'), ('second', b'b')]:
                (root / name).mkdir()
                (root / name / 'config.json').write_text('{"architectures":["QwenForCausalLM"]}')
                with (root / name / 'model.safetensors').open('wb') as handle:
                    handle.write(content)
                    handle.truncate(101 * 1024 * 1024)
            candidates = scan_tree(root).models
            self.assertEqual(len(candidates), 2)
            self.assertNotEqual(inventory_fingerprint(candidates[0]), inventory_fingerprint(candidates[1]))


class ServiceInventoryTests(unittest.TestCase):
    def test_authenticated_listener_is_adopted_once_without_creating_a_model_placeholder(self):
        h = fixtures.ReportServiceTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        service = h.service_with_account()
        report = fixtures._payload(engines=[{'port': 8000, 'framework': 'vllm', 'models': ['served-alias']}])
        service.report_agent(h.requester(), report)
        service.report_agent(h.requester(), report)
        engines = h.store.list_engines()
        self.assertEqual(len(engines), 1)
        engine = engines[0]
        self.assertEqual(engine.owner_principal, fixtures.ALICE_ACCOUNT)
        self.assertEqual(engine.ownership['uid'], fixtures.ALICE_UID)
        self.assertEqual(engine.launch['visibility'], 'private')
        self.assertEqual(h.store.list_models(), [])
        service.report_agent(h.requester(), fixtures._payload(full=False))
        self.assertEqual(h.store.engine_by_id(engine.id).state, 'attached')
        self.assertEqual(service_state(engine, now=fixtures.NOW + timedelta(hours=1)), 'stale')
        service.report_agent(h.requester(), fixtures._payload())
        self.assertEqual(h.store.engine_by_id(engine.id).state, 'offline')
