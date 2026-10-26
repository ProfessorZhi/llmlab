"""Local discovery: candidates only, read-only, bounded.

The listener table is fixture text in ``/proc/net/tcp`` format and every HTTP
answer comes from a fake fetch, so nothing here opens a socket.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.registry import local_discovery as ld_mod
from backend.registry.local_discovery import (
    DEFAULT_MODEL_ROOTS,
    LocalDiscovery,
    parse_model_ids,
    suggest_name,
)
from backend.registry.ownership import ProcNetListenerTable

# 8000 (0x1F40) uid 1001 on 127.0.0.1; 8001 (0x1F41) uid 1002 on 0.0.0.0;
# 9999 (0x270F) not in the allowlist; 8081 (0x1F91) bound to 10.0.0.5 only.
TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 1 1 0000000000000000 100 0 0 10 0
   1: 00000000:1F41 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1002        0 2 1 0000000000000000 100 0 0 10 0
   2: 0100007F:270F 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 3 1 0000000000000000 100 0 0 10 0
   3: 0500000A:1F91 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1003        0 4 1 0000000000000000 100 0 0 10 0
"""
ALLOWED = frozenset({8000, 8001, 8081, 9000, 9001})


def fake_fetch(answers):
    calls = []

    def fetch(port, path):
        calls.append((port, path))
        return answers.get((port, path))

    fetch.calls = calls
    return fetch


class EngineDiscoveryTests(unittest.TestCase):
    def setUp(self):
        models = json.dumps({"data": [{"id": "Qwen/Qwen2.5-7B-Instruct"}, {"id": "bad id with spaces"}]}).encode()
        self.fetch = fake_fetch({
            (8000, "/v1/models"): (200, models),
            (8000, "/version"): (200, b'{"version": "0.6.0"}'),
            (8001, "/v1/models"): (404, b"not here"),
        })
        self.discovery = LocalDiscovery(
            allowed_ports=ALLOWED,
            table=ProcNetListenerTable.from_texts(tcp=TCP),
            fetch=self.fetch,
            uid_namer={1001: "zhang", 1002: "li"}.get,
        )

    def test_only_allowed_loopback_listeners_become_candidates(self):
        found, problem = self.discovery.engines(registered_targets={})
        self.assertIsNone(problem)
        self.assertEqual([c.port for c in found], [8000, 8001])
        self.assertNotIn(9999, {port for port, _ in self.fetch.calls})
        self.assertNotIn(8081, {port for port, _ in self.fetch.calls}, "a non-loopback bind is never probed")

    def test_a_candidate_carries_runtime_user_models_and_framework(self):
        found, _ = self.discovery.engines(registered_targets={})
        first = found[0]
        self.assertEqual(first.run_as_user, "zhang")
        self.assertEqual(first.models, ("Qwen/Qwen2.5-7B-Instruct",), "unsafe ids are dropped")
        self.assertEqual(first.framework, "vllm")
        self.assertEqual(first.suggested_name, "Qwen2.5-7B-Instruct")
        self.assertTrue(first.openai_compatible)
        second = found[1]
        self.assertFalse(second.openai_compatible)
        self.assertIsNone(second.framework)

    def test_a_service_answering_json_everywhere_is_not_named_a_framework(self):
        anything = (200, b'{"data": [{"id": "m"}], "ok": true}')
        discovery = LocalDiscovery(
            allowed_ports=ALLOWED,
            table=ProcNetListenerTable.from_texts(tcp=TCP),
            fetch=lambda port, path: anything if port == 8000 else None,
        )
        found, _ = discovery.engines(registered_targets={})
        self.assertIsNone(found[0].framework)

    def test_registration_state_is_reported_not_changed(self):
        found, _ = self.discovery.engines(registered_targets={("127.0.0.1", 8000): True, ("127.0.0.1", 8001): False})
        self.assertTrue(found[0].registered_by_me)
        self.assertTrue(found[1].registered)
        self.assertFalse(found[1].registered_by_me)

    def test_an_unreadable_table_is_a_stated_problem(self):
        broken = LocalDiscovery(allowed_ports=ALLOWED, table=ProcNetListenerTable.from_texts(), fetch=self.fetch)
        found, problem = broken.engines(registered_targets={})
        self.assertEqual(found, [])
        self.assertEqual(problem, "proc_net_unreadable")


class ModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="llm-lab-models-"))
        self.outside = Path(tempfile.mkdtemp(prefix="llm-lab-outside-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        hf = self.root / "qwen2.5-7b"
        hf.mkdir()
        (hf / "config.json").write_text(json.dumps({"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2"}))
        (hf / "model-00001.safetensors").write_bytes(b"x" * 100)
        (hf / "model-00002.safetensors").write_bytes(b"x" * 50)
        (hf / "nested").mkdir()
        (hf / "nested" / "config.json").write_text("{}")
        (hf / "nested" / "w.bin").write_bytes(b"x")
        gguf_dir = self.root / "gguf" / "llama"
        gguf_dir.mkdir(parents=True)
        (gguf_dir / "llama-3-8b.Q4_K_M.gguf").write_bytes(b"g" * 10)
        (self.root / "half-downloaded").mkdir()
        (self.root / "half-downloaded" / "config.json").write_text("{}")
        (self.outside / "config.json").write_text("{}")
        (self.outside / "secret.safetensors").write_bytes(b"s")
        os.symlink(self.outside, self.root / "link-out")

    def discovery(self, *roots):
        return LocalDiscovery(allowed_ports=ALLOWED, model_roots=tuple(str(r) for r in roots), table=ProcNetListenerTable.from_texts(tcp=""), fetch=fake_fetch({}))

    def test_finds_hf_dirs_and_gguf_files_only(self):
        found, missing = self.discovery(self.root).models(registered_paths={})
        self.assertEqual(missing, [])
        by_format = {c.format: c for c in found}
        self.assertEqual(len(found), 2, [c.path for c in found])
        hf = by_format["hf"]
        self.assertEqual(Path(hf.path).name, "qwen2.5-7b")
        self.assertEqual(hf.size_bytes, 150)
        self.assertEqual(hf.architecture, "Qwen2ForCausalLM")
        self.assertEqual(hf.suggested_name, "qwen2.5-7b")
        self.assertEqual(by_format["gguf"].suggested_name, "llama-3-8b.Q4_K_M")

    def test_symlinks_out_of_the_root_are_not_followed(self):
        found, _ = self.discovery(self.root).models(registered_paths={})
        self.assertFalse(any(str(self.outside) in c.path or "link-out" in c.path for c in found))

    def test_registered_paths_are_marked(self):
        hf_path = str((self.root / "qwen2.5-7b").resolve())
        found, _ = self.discovery(self.root).models(registered_paths={hf_path: True})
        hf = next(c for c in found if c.format == "hf")
        self.assertTrue(hf.registered)
        self.assertTrue(hf.registered_by_me)
        self.assertFalse(next(c for c in found if c.format == "gguf").registered)

    def test_split_gguf_shards_are_one_model(self):
        """``…-00001-of-00002`` 与 ``…-00002-of-00002`` 是一个模型的两片。

        按片报会让人「登记」半个权重文件，界面还会显示成两个模型。
        """

        shard_dir = self.root / "split"
        shard_dir.mkdir()
        first = shard_dir / "big-q4-00001-of-00002.gguf"
        second = shard_dir / "big-q4-00002-of-00002.gguf"
        first.write_bytes(b"a" * 30)
        second.write_bytes(b"b" * 70)

        found, _ = self.discovery(self.root).models(registered_paths={})

        split = [c for c in found if c.path.endswith(".gguf") and "big-q4" in c.path]
        self.assertEqual([c.path for c in split], [str(first)])
        self.assertEqual(split[0].size_bytes, 100, "大小应当是各片之和")
        self.assertEqual(split[0].suggested_name, "big-q4")

    def test_a_multimodal_projector_is_not_a_model(self):
        shard_dir = self.root / "vision"
        shard_dir.mkdir()
        (shard_dir / "vision-q4-00001-of-00001.gguf").write_bytes(b"v" * 10)
        (shard_dir / "mmproj-vision-f16.gguf").write_bytes(b"p" * 10)

        found, _ = self.discovery(self.root).models(registered_paths={})

        names = [c.suggested_name for c in found]
        self.assertIn("vision-q4", names)
        self.assertFalse(any("mmproj" in name for name in names), names)

    def test_a_tokenizer_vocabulary_is_not_a_model(self):
        """``ggml-vocab-*.gguf`` 与模型同格式，但一个源码树里能躺二十几个。

        实测：一个人 42 条模型里 38 条是 llama.cpp 自带词表——清单被它们淹掉之后，
        「有哪些模型」这个问题就没人答得出来了。
        """

        vocab_dir = self.root / "llama.cpp-src" / "models"
        vocab_dir.mkdir(parents=True)
        (vocab_dir / "ggml-vocab-llama-spm.gguf").write_bytes(b"w" * 10)
        (vocab_dir / "ggml-vocab-qwen2.gguf").write_bytes(b"w" * 10)

        found, _ = self.discovery(self.root).models(registered_paths={})

        self.assertFalse(
            any("ggml-vocab" in c.path for c in found), [c.path for c in found]
        )

    def test_registering_one_shard_marks_the_whole_model(self):
        shard_dir = self.root / "split2"
        shard_dir.mkdir()
        (shard_dir / "m-00001-of-00002.gguf").write_bytes(b"a")
        (shard_dir / "m-00002-of-00002.gguf").write_bytes(b"b")

        found, _ = self.discovery(self.root).models(
            registered_paths={str(shard_dir / "m-00002-of-00002.gguf"): True}
        )

        model = next(c for c in found if c.suggested_name == "m")
        self.assertTrue(model.registered)
        self.assertTrue(model.registered_by_me)

    def test_a_missing_root_is_reported(self):
        found, missing = self.discovery(self.root / "nope").models(registered_paths={})
        self.assertEqual(found, [])
        self.assertEqual(len(missing), 1)
        self.assertTrue(missing[0].startswith("model_root_unavailable:"))


class AutomaticRootTests(unittest.TestCase):
    """「自动发现」不能要求管理员先配一个目录——那就不叫自动了。"""

    def bare(self, **kwargs):
        return LocalDiscovery(
            allowed_ports=ALLOWED,
            table=ProcNetListenerTable.from_texts(tcp=""),
            fetch=fake_fetch({}),
            **kwargs,
        )

    def test_no_explicit_root_means_the_default_locations(self):
        discovery = self.bare()

        self.assertIsNone(discovery.model_roots)
        self.assertEqual(
            discovery.effective_model_roots(),
            tuple(str(Path(root).expanduser()) for root in DEFAULT_MODEL_ROOTS),
        )

    def test_an_explicit_empty_tuple_means_do_not_scan(self):
        found, problems = self.bare(model_roots=()).models(registered_paths={})

        self.assertEqual(found, [])
        self.assertEqual(problems, [])

    def test_a_noisy_directory_is_not_walked_into(self):
        """``node_modules`` 能一口吃掉条目预算，把真正的模型挡在后面。"""

        root = Path(tempfile.mkdtemp(prefix="llm-lab-noise-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        buried = root / "node_modules" / "pkg" / "model"
        buried.mkdir(parents=True)
        (buried / "config.json").write_text("{}")
        (buried / "w.safetensors").write_bytes(b"x")

        found, _ = self.bare(model_roots=(str(root),)).models(registered_paths={})

        self.assertEqual(found, [])

    def test_hitting_the_entry_budget_is_reported_not_silent(self):
        """扫不完却不说话，页面就只剩「没有发现模型」，没人知道该查哪儿。"""

        root = Path(tempfile.mkdtemp(prefix="llm-lab-trunc-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for index in range(10):
            (root / f"f{index}.txt").write_text("x")

        with mock.patch.object(ld_mod, "MAX_WALK_ENTRIES", 3):
            found, problems = self.bare(model_roots=(str(root),)).models(registered_paths={})

        self.assertEqual(found, [])
        self.assertEqual(problems, [f"model_walk_truncated:{root}"])


class InstalledEngineTests(unittest.TestCase):
    """磁盘上装着的引擎：跑没跑都要能看见。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="llm-lab-engines-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def venv(self, *, relative="qwen-test/venv", executable=None, distributions=()):
        env = self.root / relative
        (env / "bin").mkdir(parents=True)
        (env / "pyvenv.cfg").write_text("home = /usr/bin\n")
        if executable:
            exe = env / "bin" / executable
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        for dist in distributions:
            (env / "lib" / "python3.12" / "site-packages" / dist).mkdir(parents=True)
        return env

    def discovery(self):
        return LocalDiscovery(
            allowed_ports=ALLOWED,
            model_roots=(str(self.root),),
            table=ProcNetListenerTable.from_texts(tcp=""),
            fetch=fake_fetch({}),
        )

    def test_an_engine_inside_a_virtualenv_is_found(self):
        env = self.venv(executable="vllm")

        found, _problems = self.discovery().installed_engines()

        vllm = [item for item in found if item.engine == "vllm"]
        self.assertEqual([item.found_as for item in vllm], ["executable"])
        self.assertEqual(vllm[0].path, str(env / "bin" / "vllm"))
        self.assertEqual(vllm[0].env, str(env))

    def test_a_version_is_read_from_pip_metadata_never_by_running_anything(self):
        """``vllm --version`` 得先执行一个别人放进去的程序；发现不做这件事。"""

        env = self.venv(executable="vllm", distributions=("vllm-0.6.3.dist-info",))

        found, _ = self.discovery().installed_engines()

        by_kind = {item.found_as: item for item in found if item.engine == "vllm"}
        self.assertEqual(by_kind["executable"].version, None)
        self.assertEqual(by_kind["python_package"].version, "0.6.3")
        self.assertEqual(by_kind["python_package"].env, str(env))

    def test_lib_and_lib64_are_one_installation_not_two(self):
        """``lib64`` 通常是指向 ``lib`` 的符号链接，同一份分发包会被数两遍。

        地点不同但东西相同，于是「装了几个 llama.cpp」变成 2——而人只装了一次。
        """

        env = self.venv(executable="vllm")
        site = env / "lib" / "python3.12" / "site-packages"
        (site / "llama_cpp_python-0.3.36.dist-info").mkdir(parents=True)
        os.symlink(env / "lib", env / "lib64")

        found, _ = self.discovery().installed_engines()

        llama = [item for item in found if item.engine == "llama.cpp"]
        self.assertEqual(len(llama), 1, [item.path for item in llama])
        self.assertEqual(llama[0].version, "0.3.36")

    def test_a_directory_without_pyvenv_cfg_is_not_a_virtualenv(self):
        fake = self.root / "looks-like-venv" / "bin"
        fake.mkdir(parents=True)
        exe = fake / "vllm"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)

        found, _ = self.discovery().installed_engines()

        self.assertEqual([item for item in found if item.engine == "vllm"], [])

    def test_a_source_tree_named_after_an_engine_is_reported_as_such(self):
        tree = self.root / "vllm"
        tree.mkdir()
        (tree / "CMakeLists.txt").write_text("project(vllm)\n")

        found, _ = self.discovery().installed_engines()

        vllm = [item for item in found if item.engine == "vllm"]
        self.assertEqual([item.found_as for item in vllm], ["source_tree"])
        self.assertEqual(vllm[0].path, str(tree))

    def test_an_empty_directory_named_after_an_engine_is_not_an_engine(self):
        (self.root / "sglang").mkdir()

        found, _ = self.discovery().installed_engines()

        self.assertEqual([item for item in found if item.engine == "sglang"], [])


class HelperTests(unittest.TestCase):
    def test_parse_model_ids(self):
        self.assertEqual(parse_model_ids(b'{"data":[{"id":"a"},{"id":7}]}'), ["a"])
        self.assertIsNone(parse_model_ids(b"<html>"))
        self.assertIsNone(parse_model_ids(b'{"models":[]}'))

    def test_suggest_name(self):
        self.assertEqual(suggest_name("org/My Model!"), "My-Model")
        self.assertIsNone(suggest_name(""))


if __name__ == "__main__":
    unittest.main()
