"""``builds`` 的离线测试。

所有执行都走假 runner：本文件里没有任何一行会真的启动 llama-server / vllm。
Git 与 CMake 的用例只操作 ``tempfile`` 下的目录，不读真实 HOME；help 样本放在
``fixtures/`` 里，直接当纯文本喂给 ``parse_help``。

llama.cpp 的样本逐字取自真机 ``llama-server --help``（kaidong 的 build）：
一行多个长别名、描述折行、``allowed values`` 续行都在里面，早期的固定列样本
掩盖过这几类真实排版。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.registry.discovery.builds import (
    ProbeCache,
    default_runner,
    detect_framework,
    enrich,
    find_git_commit,
    parse_help,
    probe_version,
    read_cmake_cache,
    read_help,
)
from backend.registry.discovery.types import BuildCandidate, FRAMEWORK_FORMATS

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class RecordingRunner:
    """按 ``argv[1]`` 分发的假 runner，并记下每次调用。"""

    def __init__(self, responses: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append((list(argv), timeout))
        key = argv[1] if len(argv) > 1 else ""
        return self.responses.get(key, (0, "", ""))


class TestParseLlamaHelp(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = load_fixture("llamacpp-help.txt")
        cls.parsed = parse_help("llama.cpp", cls.text)
        cls.flags = {flag.name: flag for flag in cls.parsed}

    def test_returns_tuple_of_unique_long_names(self) -> None:
        self.assertIsInstance(self.parsed, tuple)
        names = [flag.name for flag in self.parsed]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(name.startswith("--") for name in names))
        # 短选项（含只有短别名的 -ub）不单独建条目。
        self.assertNotIn("-c", self.flags)
        self.assertNotIn("-ngl", self.flags)
        self.assertNotIn("-ub", self.flags)
        self.assertNotIn("--ub", self.flags)

    def test_every_long_alias_gets_an_entry(self) -> None:
        for name in ("--gpu-layers", "--n-gpu-layers", "--predict", "--n-predict"):
            with self.subTest(name=name):
                self.assertIn(name, self.flags)
                self.assertEqual(self.flags[name].type, "int")

    def test_aliases_share_metadata(self) -> None:
        gpu = self.flags["--gpu-layers"]
        ngpu = self.flags["--n-gpu-layers"]
        self.assertEqual(
            (gpu.type, gpu.default, gpu.choices, gpu.help),
            (ngpu.type, ngpu.default, ngpu.choices, ngpu.help),
        )
        predict = self.flags["--predict"]
        npredict = self.flags["--n-predict"]
        self.assertEqual(
            (predict.type, predict.default, predict.help),
            (npredict.type, npredict.default, npredict.help),
        )

    def test_n_predict_default(self) -> None:
        self.assertEqual(self.flags["--n-predict"].default, "-1")

    def test_ctx_size_keeps_only_numeric_default_prefix(self) -> None:
        flag = self.flags["--ctx-size"]
        self.assertEqual(flag.type, "int")
        self.assertEqual(flag.default, "0")
        self.assertIn("size of the prompt context", flag.help)
        # "(env: ...)" 在下一行，必须拼回同一条 help。
        self.assertIn("LLAMA_ARG_CTX_SIZE", flag.help)

    def test_wrapped_default_is_joined_before_extraction(self) -> None:
        flag = self.flags["--keep"]
        self.assertEqual(flag.type, "int")
        self.assertEqual(flag.default, "0")
        self.assertIn("all)", flag.help)

    def test_allowed_values_become_choices(self) -> None:
        flag = self.flags["--cache-type-k"]
        self.assertEqual(flag.type, "choice")
        self.assertEqual(flag.default, "f16")
        self.assertIn("f16", flag.choices)
        self.assertIn("q8_0", flag.choices)
        self.assertEqual(
            flag.choices,
            ("f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"),
        )

    def test_negated_switch_is_not_a_separate_entry(self) -> None:
        self.assertNotIn("--no-perf", self.flags)
        self.assertNotIn("--no-escape", self.flags)
        self.assertEqual(self.flags["--perf"].type, "bool")
        self.assertEqual(self.flags["--perf"].default, "false")
        self.assertEqual(self.flags["--escape"].type, "bool")
        self.assertEqual(self.flags["--escape"].default, "true")

    def test_n_metavar_with_non_numeric_default_is_still_int(self) -> None:
        flag = self.flags["--n-gpu-layers"]
        self.assertEqual(flag.type, "int")
        # 非数字默认值原样保留，不强行转成数字。
        self.assertEqual(flag.default, "auto")

    def test_float_metavar_with_decimal_default(self) -> None:
        self.assertEqual(self.flags["--temp"].type, "float")
        self.assertEqual(self.flags["--temp"].default, "0.800000")
        self.assertEqual(self.flags["--top-p"].type, "float")

    def test_bracketed_choices_survive_real_layout(self) -> None:
        flag = self.flags["--flash-attn"]
        self.assertEqual(flag.type, "choice")
        self.assertEqual(flag.choices, ("on", "off", "auto"))

    def test_braced_choices_survive_real_layout(self) -> None:
        flag = self.flags["--split-mode"]
        self.assertEqual(flag.type, "choice")
        self.assertEqual(flag.choices, ("none", "layer", "row", "tensor"))

    def test_filename_and_host_metavars_are_str(self) -> None:
        self.assertEqual(self.flags["--model"].type, "str")
        self.assertEqual(self.flags["--model"].default, "models/7B/ggml-model-f16.gguf")
        self.assertEqual(self.flags["--host"].type, "str")

    def test_help_aliases_share_one_definition(self) -> None:
        # "-h, --help, --usage" 里的两个长别名都要出条目，且元数据一致。
        self.assertIn("--help", self.flags)
        self.assertIn("--usage", self.flags)
        self.assertEqual(self.flags["--help"].type, "bool")
        self.assertEqual(self.flags["--usage"].type, "bool")
        self.assertEqual(self.flags["--help"].help, self.flags["--usage"].help)

    def test_short_only_line_does_not_pollute_previous_help(self) -> None:
        text = (
            "    --metrics    enable metrics (default: false)\n"
            "    -ub,   N     unroll batch size (default: 512)\n"
        )
        flags = {flag.name: flag for flag in parse_help("llama.cpp", text)}
        self.assertEqual(flags["--metrics"].help, "enable metrics (default: false)")
        self.assertNotIn("-ub", flags)
        self.assertNotIn("--ub", flags)

    def test_wrapped_line_starting_with_minus_is_continuation(self) -> None:
        text = (
            "-k, --keep N         tokens to keep (default: -1,\n"
            "                      -1 = keep all)\n"
        )
        flags = {flag.name: flag for flag in parse_help("llama.cpp", text)}
        self.assertEqual(flags["--keep"].default, "-1")

    def test_empty_and_garbage_never_raise(self) -> None:
        self.assertEqual(parse_help("llama.cpp", ""), ())
        self.assertEqual(parse_help("llama.cpp", "\x00\n--\n{-}\n----- x -----"), ())
        self.assertIsInstance(parse_help("llama.cpp", "no options here"), tuple)


class TestParseVllmHelp(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = load_fixture("vllm-help.txt")
        cls.flags = {flag.name: flag for flag in parse_help("vllm", cls.text)}

    def test_max_model_len_collects_wrapped_description(self) -> None:
        flag = self.flags["--max-model-len"]
        # argparse 的 metavar 全大写，类型只能由 default 推；default 是 None。
        self.assertEqual(flag.type, "unknown")
        self.assertEqual(flag.default, "None")
        self.assertIn("Model context length", flag.help)
        self.assertIn("automatically derived", flag.help)

    def test_dtype_is_choice(self) -> None:
        flag = self.flags["--dtype"]
        self.assertEqual(flag.type, "choice")
        self.assertEqual(flag.choices, ("auto", "bfloat16", "float", "float16", "half"))
        self.assertEqual(flag.default, "auto")

    def test_uppercase_metavar_infers_type_from_default(self) -> None:
        self.assertEqual(self.flags["--port"].type, "int")
        self.assertEqual(self.flags["--port"].default, "8000")
        self.assertEqual(self.flags["--tensor-parallel-size"].type, "int")
        self.assertEqual(self.flags["--gpu-memory-utilization"].type, "float")
        self.assertEqual(self.flags["--gpu-memory-utilization"].default, "0.9")

    def test_boolean_optional_pair(self) -> None:
        flag = self.flags["--enable-prefix-caching"]
        self.assertEqual(flag.type, "bool")
        self.assertNotIn("--no-enable-prefix-caching", self.flags)

    def test_store_true_with_wrapped_help_is_bool(self) -> None:
        flag = self.flags["--enforce-eager"]
        self.assertEqual(flag.type, "bool")
        self.assertEqual(flag.default, "False")
        self.assertIn("cudagraph", flag.help)

    def test_unknown_framework_uses_generic_argparse(self) -> None:
        generic = {flag.name: flag for flag in parse_help("unknown", self.text)}
        self.assertEqual(generic["--dtype"], self.flags["--dtype"])
        self.assertEqual(generic["--max-model-len"], self.flags["--max-model-len"])
        self.assertEqual(generic["--port"].type, "int")

    def test_sglang_uses_same_parser(self) -> None:
        sglang = {flag.name: flag for flag in parse_help("sglang", self.text)}
        self.assertEqual(sglang["--max-model-len"].name, "--max-model-len")


class TestProbeVersion(unittest.TestCase):
    def setUp(self) -> None:
        self.binary = Path("/no/such/dir/llama-server")

    def probe(self, response: tuple[int, str, str], timeout: float = 5.0) -> tuple[object, RecordingRunner]:
        runner = RecordingRunner({"--version": response})
        result = probe_version(self.binary, timeout=timeout, runner=runner)
        return result, runner

    def test_llama_cpp_build_number_wins(self) -> None:
        version, _ = self.probe((0, "llama-server version: b6512 (d1f2a3b)\n", ""))
        self.assertEqual(version, "b6512")

    def test_semver_fallback(self) -> None:
        version, _ = self.probe((0, "0.6.3\n", ""))
        self.assertEqual(version, "0.6.3")
        version, _ = self.probe((0, "", "vLLM 0.6.3.post1\n"))
        self.assertEqual(version, "0.6.3")

    def test_first_nonempty_line_fallback_is_truncated(self) -> None:
        long_line = "x" * 400
        version, _ = self.probe((0, f"\n\n{long_line}\n", ""))
        self.assertEqual(version, "x" * 120)

    def test_timeout_returns_none_not_reason_text(self) -> None:
        version, _ = self.probe((-1, "", "timeout after 5s"))
        self.assertIsNone(version)

    def test_nonzero_exit_returns_none(self) -> None:
        version, _ = self.probe((1, "fatal: cannot initialize CUDA\n", ""))
        self.assertIsNone(version)

    def test_empty_output_returns_none(self) -> None:
        version, _ = self.probe((0, "", ""))
        self.assertIsNone(version)
        version, _ = self.probe((0, "   \n\t\n", ""))
        self.assertIsNone(version)

    def test_invocation_shape(self) -> None:
        _, runner = self.probe((0, "b1", ""), timeout=1.5)
        self.assertEqual(runner.calls, [([str(self.binary), "--version"], 1.5)])


class TestReadHelp(unittest.TestCase):
    def setUp(self) -> None:
        self.binary = Path("/no/such/dir/vllm")

    def test_concatenates_stdout_and_stderr(self) -> None:
        runner = RecordingRunner({"--help": (0, "usage: vllm", "extra note")})
        text = read_help(self.binary, runner=runner)
        self.assertEqual(text, "usage: vllm\nextra note")
        self.assertEqual(runner.calls, [([str(self.binary), "--help"], 5.0)])

    def test_accepts_nonzero_exit_with_output(self) -> None:
        runner = RecordingRunner({"--help": (2, "usage: broken", "")})
        self.assertEqual(read_help(self.binary, runner=runner), "usage: broken")

    def test_timeout_returns_none(self) -> None:
        runner = RecordingRunner({"--help": (-1, "", "timeout after 5s")})
        self.assertIsNone(read_help(self.binary, runner=runner))

    def test_empty_output_returns_none(self) -> None:
        runner = RecordingRunner({"--help": (0, "", "")})
        self.assertIsNone(read_help(self.binary, runner=runner))

    def test_truncates_at_256k_chars(self) -> None:
        runner = RecordingRunner({"--help": (0, "y" * (300 * 1024), "")})
        text = read_help(self.binary, runner=runner)
        self.assertIsNotNone(text)
        assert text is not None
        self.assertEqual(len(text), 256 * 1024)

    def test_custom_timeout_is_passed_through(self) -> None:
        runner = RecordingRunner({"--help": (0, "usage", "")})
        read_help(self.binary, timeout=0.25, runner=runner)
        self.assertEqual(runner.calls[0][1], 0.25)


class TestDefaultRunner(unittest.TestCase):
    """只验证失败路径：真的执行二进制不在测试范围内。"""

    def test_missing_binary_returns_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            returncode, stdout, stderr = default_runner([str(missing)], 0.2)
        self.assertEqual(returncode, -1)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr)

    def test_empty_argv(self) -> None:
        self.assertEqual(default_runner([], 1.0)[0], -1)


class TestFindGitCommit(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_normal_repository_from_nested_directory(self) -> None:
        sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
        write(self.root / "repo" / ".git" / "HEAD", "ref: refs/heads/main\n")
        write(self.root / "repo" / ".git" / "refs" / "heads" / "main", sha + "\n")
        start = self.root / "repo" / "build" / "bin"
        start.mkdir(parents=True)
        self.assertEqual(find_git_commit(start), sha)

    def test_detached_head(self) -> None:
        sha = "b" * 40
        write(self.root / ".git" / "HEAD", sha + "\n")
        self.assertEqual(find_git_commit(self.root), sha)

    def test_packed_refs(self) -> None:
        sha = "0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b"
        write(self.root / ".git" / "HEAD", "ref: refs/heads/dev\n")
        write(
            self.root / ".git" / "packed-refs",
            "# pack-refs with: peeled fully-peeled sorted\n"
            "1111111111111111111111111111111111111111 refs/heads/other\n"
            f"{sha} refs/heads/dev\n"
            "^2222222222222222222222222222222222222222\n",
        )
        self.assertEqual(find_git_commit(self.root), sha)

    def test_worktree_gitdir_file_with_commondir(self) -> None:
        sha = "c" * 40
        main_git = self.root / "main" / ".git"
        write(main_git / "HEAD", "ref: refs/heads/main\n")
        write(main_git / "refs" / "heads" / "feature", sha + "\n")
        worktree = main_git / "worktrees" / "wt"
        write(worktree / "HEAD", "ref: refs/heads/feature\n")
        write(worktree / "commondir", "../..\n")
        project = self.root / "project"
        write(project / ".git", f"gitdir: {worktree}\n")
        (project / "src").mkdir(parents=True)
        self.assertEqual(find_git_commit(project / "src"), sha)

    def test_no_repository(self) -> None:
        start = self.root / "plain" / "deep"
        start.mkdir(parents=True)
        self.assertIsNone(find_git_commit(start))

    def test_max_up_limits_search(self) -> None:
        sha = "d" * 40
        repo = self.root / "repo"
        write(repo / ".git" / "HEAD", sha + "\n")
        start = repo / "x" / "y" / "z"
        start.mkdir(parents=True)
        self.assertIsNone(find_git_commit(start, max_up=2))
        self.assertEqual(find_git_commit(start, max_up=3), sha)

    def test_bad_head_returns_none(self) -> None:
        write(self.root / ".git" / "HEAD", "not a hash\n")
        self.assertIsNone(find_git_commit(self.root))

    def test_missing_ref_returns_none(self) -> None:
        write(self.root / ".git" / "HEAD", "ref: refs/heads/gone\n")
        self.assertIsNone(find_git_commit(self.root))


class TestReadCmakeCache(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_filters_keys_and_keeps_raw_values(self) -> None:
        write(
            self.root / "CMakeCache.txt",
            "# This is the CMakeCache file.\n"
            "// comment\n"
            "GGML_CUDA:BOOL=ON\n"
            "GGML_NATIVE:BOOL=OFF\n"
            "LLAMA_CURL:BOOL=ON\n"
            "GGML_EXTRA_FLAGS:STRING=-DFOO=1\n"
            "CMAKE_BUILD_TYPE:STRING=Release\n"
            "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/c++\n"
            "SOME_OTHER_KEY:STRING=ignored\n",
        )
        nested = self.root / "build" / "bin"
        nested.mkdir(parents=True)
        cache = read_cmake_cache(nested)
        self.assertEqual(
            cache,
            {
                "GGML_CUDA": "ON",
                "GGML_NATIVE": "OFF",
                "LLAMA_CURL": "ON",
                "GGML_EXTRA_FLAGS": "-DFOO=1",
                "CMAKE_BUILD_TYPE": "Release",
            },
        )

    def test_missing_cache_is_empty(self) -> None:
        self.assertEqual(read_cmake_cache(self.root), {})


class TestDetectFramework(unittest.TestCase):
    def test_basename_mapping(self) -> None:
        cases = {
            "/opt/llama.cpp/build/bin/llama-server": "llama.cpp",
            "/data/bin/strata": "strata",
            "/venv/bin/vllm": "vllm",
            "/venv/bin/sglang": "sglang",
            "/usr/local/bin/ollama": "ollama",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(detect_framework(Path(path)), expected)

    def test_unknown_basename(self) -> None:
        self.assertIsNone(detect_framework(Path("/opt/bin/mystery-engine")))


class TestProbeCacheAndEnrich(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.llama_help = load_fixture("llamacpp-help.txt")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self) -> RecordingRunner:
        return RecordingRunner(
            {
                "--version": (0, "llama-server version: b6512\n", ""),
                "--help": (0, self.llama_help, ""),
            }
        )

    def test_enrich_fills_everything_and_caches(self) -> None:
        binary = self.root / "bin" / "llama-server"
        binary.parent.mkdir(parents=True)
        binary.write_text("", encoding="utf-8")
        candidate = BuildCandidate(binary=binary, framework="llama.cpp")
        runner = self._runner()
        cache = ProbeCache()

        first = enrich(candidate, runner=runner, cache=cache)
        self.assertEqual(first.version, "b6512")
        self.assertEqual(first.formats, ("gguf",))
        self.assertIsNone(first.git_commit)
        self.assertEqual(dict(first.cmake), {})
        flags = {flag.name: flag for flag in first.flags}
        self.assertEqual(flags["--ctx-size"].type, "int")
        self.assertEqual(flags["--n-gpu-layers"].type, "int")
        self.assertEqual(len(runner.calls), 2)

        # 第二次命中缓存：一次执行都不该发生。
        second = enrich(candidate, runner=runner, cache=cache)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(second.version, first.version)
        self.assertEqual(second.flags, first.flags)

        # 二进制被重建（mtime 变了）后缓存必须失效。
        os.utime(binary, ns=(10**18, 10**18))
        third = enrich(candidate, runner=runner, cache=cache)
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(third.version, "b6512")

    def test_unknown_framework_is_detected_from_basename(self) -> None:
        binary = self.root / "strata"
        binary.write_text("", encoding="utf-8")
        candidate = BuildCandidate(binary=binary, framework="unknown")
        runner = self._runner()
        result = enrich(candidate, runner=runner, cache=ProbeCache())
        self.assertEqual(result.framework, "strata")
        self.assertEqual(result.formats, ("gguf",))
        self.assertEqual(result.version, "b6512")
        self.assertEqual(len(runner.calls), 2)

    def test_vllm_formats(self) -> None:
        binary = self.root / "vllm"
        binary.write_text("", encoding="utf-8")
        candidate = BuildCandidate(binary=binary, framework="vllm")
        result = enrich(candidate, runner=self._runner(), cache=ProbeCache())
        self.assertEqual(result.formats, FRAMEWORK_FORMATS["vllm"])
        self.assertEqual(result.formats, ("hf",))

    def test_unknown_basename_executes_nothing(self) -> None:
        binary = self.root / "mystery-engine"
        binary.write_text("", encoding="utf-8")
        candidate = BuildCandidate(binary=binary, framework="unknown")
        calls: list[list[str]] = []

        def forbidden(argv: list[str], timeout: float) -> tuple[int, str, str]:
            calls.append(argv)
            raise AssertionError("unknown build must not be executed")

        result = enrich(candidate, runner=forbidden, cache=ProbeCache())
        self.assertIs(result, candidate)
        self.assertEqual(calls, [])
        self.assertEqual(result.formats, ())
        self.assertIsNone(result.version)

    def test_cached_none_is_a_hit(self) -> None:
        binary = self.root / "llama-server"
        binary.write_text("", encoding="utf-8")
        cache = ProbeCache()
        self.assertEqual(cache.lookup_version(binary), (False, None))
        cache.store_version(binary, None)
        self.assertEqual(cache.lookup_version(binary), (True, None))

    def test_missing_binary_is_not_cached(self) -> None:
        binary = self.root / "absent"
        cache = ProbeCache()
        cache.store_version(binary, "b1")
        self.assertEqual(cache.lookup_version(binary), (False, None))


if __name__ == "__main__":
    unittest.main()
