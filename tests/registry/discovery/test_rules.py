"""§5.2 扫描规则的验收测试。

全部在 ``tempfile.TemporaryDirectory`` 里离线运行：不读 HOME、不联网、
不执行 fixture 里的任何“二进制”（它们只是带执行位的 shell 文本）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.registry.discovery.rules import (
    DEFAULT_EXCLUDED_DIRS,
    Limits,
    scan_tree,
)
from backend.registry.discovery.types import MIN_MODEL_BYTES
from tests.registry.discovery.fixture_tree import (
    CACHE_SHARD_BYTES,
    EXPECTED_BUILD_COUNT,
    EXPECTED_MODEL_COUNT,
    build_kaidong_like_tree,
)

MB = 1024 * 1024


def _sparse(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


def _text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _executable(path: Path) -> Path:
    _text(path, "#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


class ScanTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name) / "home"
        self.base.mkdir(parents=True, exist_ok=True)

    # -- S1 验收：仿 kaidong 目录 -----------------------------------------

    def test_kaidong_fixture_has_exactly_four_models_and_two_builds(self) -> None:
        build_kaidong_like_tree(self.base)
        result = scan_tree(self.base)

        self.assertEqual(len(result.models), EXPECTED_MODEL_COUNT)
        self.assertEqual(len(result.builds), EXPECTED_BUILD_COUNT)

        expected_models = {
            self.base / "models" / "one" / "big-one.gguf",
            self.base / "models" / "two" / "big-two.gguf",
            self.base / "hf" / "two-shard",
            self.base / "hf-cache" / "hub" / "models--org--name" / "snapshots" / "rev1",
        }
        self.assertEqual({model.path for model in result.models}, expected_models)
        self.assertEqual(sorted(model.kind for model in result.models), ["gguf", "gguf", "hf", "hf"])

        expected_builds = {
            self.base / "third_party" / "llama.cpp" / "build" / "bin" / "llama-server": "llama.cpp",
            self.base / "venv" / "bin" / "vllm": "vllm",
        }
        self.assertEqual({build.binary: build.framework for build in result.builds}, expected_builds)

        # 输出按路径排序，保证上报与 --explain 的 diff 稳定。
        self.assertEqual(
            [str(model.path) for model in result.models],
            sorted(str(model.path) for model in result.models),
        )
        self.assertEqual(
            [str(build.binary) for build in result.builds],
            sorted(str(build.binary) for build in result.builds),
        )

    def test_hf_cache_symlink_reports_real_blob_size(self) -> None:
        build_kaidong_like_tree(self.base)
        result = scan_tree(self.base)
        models = {model.path: model for model in result.models}

        snapshot = self.base / "hf-cache" / "hub" / "models--org--name" / "snapshots" / "rev1"
        cached = models[snapshot]
        self.assertEqual(cached.kind, "hf")
        self.assertEqual(cached.size_bytes, CACHE_SHARD_BYTES)
        self.assertEqual(cached.config, snapshot / "config.json")
        self.assertEqual(len(cached.weights), 1)
        self.assertEqual(cached.weights[0].size, CACHE_SHARD_BYTES)

        two_shard = models[self.base / "hf" / "two-shard"]
        self.assertEqual(two_shard.kind, "hf")
        self.assertEqual(two_shard.size_bytes, 120 * MB)
        self.assertEqual(len(two_shard.weights), 2)

    def test_mmproj_attached_and_never_a_model(self) -> None:
        build_kaidong_like_tree(self.base)
        result = scan_tree(self.base)
        models = {model.path: model for model in result.models}

        big_two = models[self.base / "models" / "two" / "big-two.gguf"]
        self.assertEqual(big_two.mmproj, self.base / "models" / "two" / "mmproj-big-two.gguf")
        self.assertIsNone(models[self.base / "models" / "one" / "big-one.gguf"].mmproj)

        mmproj = self.base / "models" / "two" / "mmproj-big-two.gguf"
        self.assertNotIn(mmproj, {model.path for model in result.models})
        self.assertNotIn(mmproj, {record.path for record in result.skipped})

    def test_source_trees_do_not_leak_models(self) -> None:
        build_kaidong_like_tree(self.base)
        result = scan_tree(self.base)

        excluded_dirs = [
            self.base / "third_party" / "llama.cpp" / "models",
            self.base / "src" / "git-tree" / ".git",
            self.base / "src" / "node-tree" / "node_modules",
            self.base / "src" / "venv-tree" / ".venv",
            self.base / "src" / "cmake-tree" / "CMakeFiles",
            self.base / "src" / "pycache-tree" / "__pycache__",
        ]
        reasons = {record.path: record.reason for record in result.skipped}
        for excluded in excluded_dirs:
            self.assertEqual(reasons.get(excluded), "excluded_dir", f"{excluded} 应被排除")

        # 六棵源码树里的诱饵全都叫 model.gguf；一个都不该出现在结果里。
        self.assertEqual(
            [model.path for model in result.models if model.path.name == "model.gguf"],
            [],
        )

        # site-packages 里的可执行 vllm 也是诱饵。
        site_packages = self.base / "vendor" / "site-packages"
        self.assertEqual(reasons.get(site_packages), "excluded_dir")
        self.assertNotIn(site_packages / "vllm", {build.binary for build in result.builds})

    def test_noise_records_vocab_and_below_min_size(self) -> None:
        build_kaidong_like_tree(self.base)
        result = scan_tree(self.base)
        records = {(record.path, record.reason) for record in result.skipped}

        self.assertIn(
            (self.base / "noise" / "vocab-a" / "ggml-vocab-a.gguf", "vocab_file"),
            records,
        )
        self.assertIn(
            (self.base / "noise" / "vocab-b" / "ggml-vocab-b.gguf", "vocab_file"),
            records,
        )
        self.assertIn(
            (self.base / "noise" / "small-gguf" / "tiny-10mb.gguf", "below_min_size"),
            records,
        )
        self.assertIn(
            (self.base / "noise" / "hf-small", "below_min_size"),
            records,
        )
        # 10 MB < MIN_MODEL_BYTES；防止 fixture 常量漂移。
        self.assertLess(10 * MB, MIN_MODEL_BYTES)

    # -- 单规则小树 ---------------------------------------------------------

    def test_diffusers_is_recognized_and_not_descended(self) -> None:
        model_dir = self.base / "diffusers" / "tiny-sd"
        _text(model_dir / "model_index.json", '{"_class_name": "StableDiffusionPipeline"}')
        _sparse(model_dir / "unet" / "diffusion_pytorch_model.safetensors", 2 * MB)
        _sparse(model_dir / "vae" / "diffusion_pytorch_model.safetensors", 3 * MB)
        # 命中 model_index.json 后不再深入；这个 GGUF 不该变成独立模型。
        _sparse(model_dir / "unet" / "nested.gguf", 200 * MB)

        result = scan_tree(self.base)

        self.assertEqual(len(result.models), 1)
        model = result.models[0]
        self.assertEqual(model.path, model_dir)
        self.assertEqual(model.kind, "diffusers")
        self.assertEqual(model.size_bytes, 5 * MB)
        self.assertEqual(model.config, model_dir / "model_index.json")
        self.assertEqual(len(model.weights), 2)
        self.assertEqual(model.mmproj, None)

    def test_cache_only_allows_huggingface_hub(self) -> None:
        hub = self.base / ".cache" / "huggingface" / "hub" / "models--a--b" / "snapshots" / "rev"
        _text(hub / "config.json", '{"model_type": "fixture"}')
        _sparse(hub / "model.safetensors", 120 * MB)

        _sparse(self.base / ".cache" / "pip" / "big.gguf", 200 * MB)
        _sparse(self.base / ".cache" / "torch" / "big.gguf", 200 * MB)
        _sparse(self.base / ".cache" / "loose.gguf", 200 * MB)
        _sparse(self.base / ".local" / "share" / "uv" / "models" / "big.gguf", 200 * MB)
        _sparse(self.base / "visible" / "big.gguf", 200 * MB)

        result = scan_tree(self.base)

        self.assertEqual(
            {model.path for model in result.models},
            {hub, self.base / "visible" / "big.gguf"},
        )
        reasons = {(record.path, record.reason) for record in result.skipped}
        for path in (
            self.base / ".cache" / "pip",
            self.base / ".cache" / "torch",
            self.base / ".cache" / "loose.gguf",
            self.base / ".local" / "share" / "uv",
        ):
            self.assertIn((path, "excluded_dir"), reasons)
        # 通往 hub 的路径本身不能被误杀。
        self.assertNotIn(self.base / ".cache" / "huggingface", {r.path for r in result.skipped})
        self.assertNotIn(self.base / ".cache" / "huggingface" / "hub", {r.path for r in result.skipped})

    def test_ignore_globs_match_basename_and_posix_path(self) -> None:
        _sparse(self.base / "good" / "a.gguf", 200 * MB)
        _sparse(self.base / "data" / "raw" / "b.gguf", 200 * MB)
        _sparse(self.base / "tmpdir" / "c.gguf", 200 * MB)

        result = scan_tree(self.base, ignore_globs=("data/raw/*.gguf", "tmpdir"))

        self.assertEqual({model.path for model in result.models}, {self.base / "good" / "a.gguf"})
        reasons = {(record.path, record.reason) for record in result.skipped}
        self.assertIn((self.base / "data" / "raw" / "b.gguf", "ignored_glob"), reasons)
        self.assertIn((self.base / "tmpdir", "ignored_glob"), reasons)

    def test_broken_symlinks_are_skipped_not_fatal(self) -> None:
        os.symlink(self.base / "missing-target.gguf", self.base / "broken.gguf")
        os.symlink(self.base / "missing-dir", self.base / "broken-dir")
        _sparse(self.base / "ok.gguf", 200 * MB)

        result = scan_tree(self.base)

        self.assertEqual({model.path for model in result.models}, {self.base / "ok.gguf"})
        reasons = {(record.path, record.reason) for record in result.skipped}
        self.assertIn((self.base / "broken.gguf", "unreadable"), reasons)
        self.assertIn((self.base / "broken-dir", "unreadable"), reasons)

    def test_missing_root_is_unreadable_not_fatal(self) -> None:
        result = scan_tree(self.base / "does-not-exist")
        self.assertEqual(result.models, ())
        self.assertEqual(result.builds, ())
        self.assertEqual(
            [(record.path, record.reason) for record in result.skipped],
            [(self.base / "does-not-exist", "unreadable")],
        )

    def test_hardlink_second_path_is_duplicate(self) -> None:
        first = _sparse(self.base / "a.gguf", 200 * MB)
        os.link(first, self.base / "b.gguf")

        result = scan_tree(self.base)

        self.assertEqual([model.path for model in result.models], [first])
        reasons = {(record.path, record.reason) for record in result.skipped}
        self.assertIn((self.base / "b.gguf", "duplicate"), reasons)

    @unittest.skipIf(os.geteuid() == 0, "root 不受权限位约束")
    def test_unreadable_dir_is_skipped(self) -> None:
        locked = self.base / "locked"
        _sparse(locked / "big.gguf", 200 * MB)
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)

        result = scan_tree(self.base)

        self.assertEqual(result.models, ())
        reasons = {(record.path, record.reason) for record in result.skipped}
        self.assertIn((locked, "unreadable"), reasons)

    def test_sglang_installed_only_as_a_package_is_still_a_build(self) -> None:
        """§5.5：``python -m sglang.launch_server`` 以包存在判定。

        SGLang 的常见装法只往 site-packages 里放包、不生成 console script，
        这时要认出来，并记住真正该执行的 argv 前缀。
        """

        env = self.base / "conda" / "env"
        interpreter = _executable(env / "bin" / "python")
        package = env / "lib" / "python3.12" / "site-packages" / "sglang"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")

        result = scan_tree(self.base)
        builds = [b for b in result.builds if b.framework == "sglang"]

        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0].binary, interpreter)
        self.assertEqual(builds[0].argv_prefix, ("-m", "sglang.launch_server"))

    def test_repeated_scans_are_identical(self) -> None:
        build_kaidong_like_tree(self.base)
        self.assertEqual(scan_tree(self.base), scan_tree(self.base))

    def test_limits_stop_descent_without_raising(self) -> None:
        deep = self.base / "a" / "b" / "c"
        _sparse(deep / "deep.gguf", 200 * MB)

        shallow = scan_tree(self.base, limits=Limits(max_depth=2))
        exact = scan_tree(self.base, limits=Limits(max_depth=3))
        tight = scan_tree(self.base, limits=Limits(max_entries=1))

        self.assertEqual(shallow.models, ())
        self.assertEqual({model.path for model in exact.models}, {deep / "deep.gguf"})
        self.assertEqual(tight.models, ())

    def test_default_excluded_dirs_contents(self) -> None:
        self.assertEqual(
            DEFAULT_EXCLUDED_DIRS,
            frozenset({".git", "node_modules", ".venv", "venv", "site-packages", "__pycache__", "CMakeFiles"}),
        )


if __name__ == "__main__":
    unittest.main()
