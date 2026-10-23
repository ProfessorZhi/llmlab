"""§5.4 facts 的合成 fixture 测试。

所有 GGUF 都在临时目录里用 ``struct`` 现场拼出来，HF / diffusers 的 JSON 也是
现写的：测试不读真实 HOME、不碰任何真实模型，也不依赖 torch。

``make_gguf`` 的取值约定（方便表达 GGUF 的类型信息）：

* ``str`` / ``bool`` / ``int`` / ``float`` -> string / bool / int32 / float32
* ``("uint32", 15)`` 等 -> 指定标量类型
* ``("array", "string", [...])`` -> 正常数组
* ``("array_declared", "uint32", 2_000_000)`` -> 只写声明、不写元素（损坏样本）
* ``("string_declared", n)`` -> 只写长度、不写字节（损坏样本）
* ``("unknown_type", 999)`` -> 未知 KV 类型
"""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.registry.discovery.facts import (
    infer_task,
    read_diffusers,
    read_gguf,
    read_hf,
    read_model,
)
from backend.registry.discovery.types import ModelCandidate

MAGIC = b"GGUF"

#: 测试侧的类型表。type 8 / 9 由 _encode_value 单独处理。
SCALAR_TYPES = {
    "uint8": (0, "<B"),
    "int8": (1, "<b"),
    "uint16": (2, "<H"),
    "int16": (3, "<h"),
    "uint32": (4, "<I"),
    "int32": (5, "<i"),
    "float32": (6, "<f"),
    "bool": (7, "<?"),
    "uint64": (10, "<Q"),
    "int64": (11, "<q"),
    "float64": (12, "<d"),
}


def _encode_string(text: str) -> bytes:
    data = text.encode("utf-8")
    return struct.pack("<Q", len(data)) + data


def _scalar_type(tag: str) -> tuple[int, str | None]:
    if tag == "string":
        return 8, None
    return SCALAR_TYPES[tag]


def _encode_typed(tag: str, value) -> tuple[int, bytes]:
    type_id, fmt = _scalar_type(tag)
    if fmt is None:
        return type_id, _encode_string(value)
    return type_id, struct.pack(fmt, value)


def _encode_value(spec) -> tuple[int, bytes]:
    if isinstance(spec, tuple) and spec:
        tag = spec[0]
        if tag == "array":
            element_type, _ = _scalar_type(spec[1])
            parts = [struct.pack("<IQ", element_type, len(spec[2]))]
            parts.extend(_encode_typed(spec[1], value)[1] for value in spec[2])
            return 9, b"".join(parts)
        if tag == "array_declared":
            element_type, _ = _scalar_type(spec[1])
            return 9, struct.pack("<IQ", element_type, spec[2])
        if tag == "string_declared":
            return 8, struct.pack("<Q", spec[1])
        if tag == "unknown_type":
            return spec[1], b""
        return _encode_typed(tag, spec[1])
    if isinstance(spec, bool):
        return _encode_typed("bool", spec)
    if isinstance(spec, int):
        return _encode_typed("int32", spec)
    if isinstance(spec, float):
        return _encode_typed("float32", spec)
    if isinstance(spec, str):
        return _encode_typed("string", spec)
    raise TypeError(f"无法编码的 GGUF 值: {spec!r}")


def build_gguf(kv: dict, *, version: int = 3, magic: bytes = MAGIC) -> bytes:
    body = bytearray()
    for key, spec in kv.items():
        body += _encode_string(key)
        type_id, payload = _encode_value(spec)
        body += struct.pack("<I", type_id)
        body += payload
    return magic + struct.pack("<IQQ", version, 0, len(kv)) + bytes(body)


def make_gguf(directory, kv: dict, *, name: str = "model.gguf", **kwargs) -> Path:
    path = Path(directory) / name
    path.write_bytes(build_gguf(kv, **kwargs))
    return path


def _quote(text: str) -> bytes:
    """给损坏用例算偏移用；和 GGUF 的 string 编码完全一致。"""
    return _encode_string(text)


class _TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_json(self, relative: str, data, *, directory: Path | None = None) -> Path:
        path = (directory or self.tmp) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class GgufFixtureTest(_TempDirTest):
    FULL_KV = {
        "general.architecture": "qwen3",
        "general.name": "Synthetic Qwen3",
        "general.file_type": ("uint32", 15),
        "qwen3.context_length": ("uint32", 40960),
        "qwen3.block_count": ("uint32", 36),
        "qwen3.embedding_length": ("uint32", 2560),
        "qwen3.attention.head_count": ("uint32", 32),
        "qwen3.attention.head_count_kv": ("uint32", 8),
        "qwen3.expert_count": ("uint32", 0),
    }

    def test_full_metadata(self) -> None:
        path = make_gguf(self.tmp, self.FULL_KV)

        facts = read_gguf(path)

        self.assertEqual(facts.format, "gguf")
        self.assertEqual(facts.architecture, "qwen3")
        self.assertEqual(facts.name, "Synthetic Qwen3")
        self.assertEqual(facts.file_type, "Q4_K_M")
        self.assertEqual(facts.context_length, 40960)
        self.assertEqual(facts.block_count, 36)
        self.assertEqual(facts.embedding_length, 2560)
        self.assertEqual(facts.head_count, 32)
        self.assertEqual(facts.head_count_kv, 8)
        # 0 是合法的专家数，不能被当成缺失。
        self.assertEqual(facts.expert_count, 0)
        self.assertFalse(facts.vision)
        self.assertEqual(facts.task, "unknown")
        self.assertEqual(facts.raw["general.architecture"], "qwen3")
        self.assertEqual(facts.raw["general.file_type"], "15")
        self.assertNotIn("error", facts.raw)

    def test_missing_fields_are_none(self) -> None:
        path = make_gguf(self.tmp, {"general.architecture": "llama"})

        facts = read_gguf(path)

        self.assertEqual(facts.architecture, "llama")
        self.assertIsNone(facts.name)
        self.assertIsNone(facts.file_type)
        self.assertIsNone(facts.context_length)
        self.assertIsNone(facts.block_count)
        self.assertIsNone(facts.embedding_length)
        self.assertIsNone(facts.head_count)
        self.assertIsNone(facts.head_count_kv)
        self.assertIsNone(facts.expert_count)
        self.assertNotIn("error", facts.raw)

    def test_file_type_mapping(self) -> None:
        cases = {
            0: "F32",
            1: "F16",
            2: "Q4_0",
            4: "4",  # 已废弃的值：不猜，原样给整数
            7: "Q8_0",
            15: "Q4_K_M",
            18: "Q6_K",
            19: "IQ2_XXS",
            99: "99",
        }
        for number, expected in cases.items():
            with self.subTest(number=number):
                path = make_gguf(
                    self.tmp,
                    {
                        "general.architecture": "llama",
                        "general.file_type": ("uint32", number),
                    },
                )
                self.assertEqual(read_gguf(path).file_type, expected)

    def test_arrays_and_preview(self) -> None:
        tokens = [f"t{index:02d}" for index in range(20)]
        path = make_gguf(
            self.tmp,
            {
                "general.architecture": "qwen3",
                "tokenizer.ggml.tokens": ("array", "string", tokens),
                "some.ints": ("array", "int32", [7, 8, 9]),
                # SD 一类模型按层给头数；这里固定「取第一个」。
                "qwen3.attention.head_count": ("array", "uint32", [32, 32, 16]),
            },
        )

        facts = read_gguf(path)

        self.assertIsNone(facts.raw.get("error"))
        self.assertEqual(facts.head_count, 32)
        self.assertIn("t00", facts.raw["tokenizer.ggml.tokens"])
        self.assertEqual(facts.raw["some.ints"], "[7, 8, 9]")
        # 预览只留前 16 个，其余元素虽然被读掉但不进 raw。
        self.assertNotIn("t19", facts.raw["tokenizer.ggml.tokens"])

    def test_truncated_file_keeps_parsed_keys(self) -> None:
        kv = {"general.architecture": "qwen3", "general.name": "Synthetic"}
        blob = build_gguf(kv)
        header_size = 4 + 4 + 8 + 8
        first_entry = (
            _quote("general.architecture")
            + struct.pack("<I", 8)
            + _quote("qwen3")
        )
        # 只把第二个 key 的长度字节截掉一半，制造文件中断。
        truncated = blob[: header_size + len(first_entry) + 3]
        path = self.tmp / "truncated.gguf"
        path.write_bytes(truncated)

        facts = read_gguf(path)

        self.assertEqual(facts.architecture, "qwen3")
        self.assertIsNone(facts.name)
        self.assertEqual(facts.raw["error"], "truncated")

    def test_bad_magic(self) -> None:
        path = make_gguf(self.tmp, {"general.architecture": "qwen3"}, magic=b"XXXX")

        facts = read_gguf(path)

        self.assertIsNone(facts.architecture)
        self.assertEqual(facts.raw["error"], "bad_magic")

    def test_unsupported_version(self) -> None:
        path = make_gguf(self.tmp, {"general.architecture": "qwen3"}, version=4)

        facts = read_gguf(path)

        self.assertIsNone(facts.architecture)
        self.assertEqual(facts.raw["error"], "unsupported_version")

    def test_unknown_kv_type_keeps_previous_keys(self) -> None:
        path = make_gguf(
            self.tmp,
            {
                "general.architecture": "qwen3",
                "mystery.key": ("unknown_type", 999),
            },
        )

        facts = read_gguf(path)

        self.assertEqual(facts.architecture, "qwen3")
        self.assertNotIn("mystery.key", facts.raw)
        self.assertEqual(facts.raw["error"], "unknown_type")

    def test_oversized_array_declaration(self) -> None:
        path = make_gguf(
            self.tmp,
            {
                "general.architecture": "qwen3",
                "tokenizer.ggml.tokens": ("array_declared", "string", 2_000_000),
            },
        )

        facts = read_gguf(path)

        self.assertEqual(facts.architecture, "qwen3")
        self.assertEqual(facts.raw["error"], "array_too_large")

    def test_oversized_string_declaration(self) -> None:
        path = make_gguf(
            self.tmp,
            {
                "general.architecture": "qwen3",
                "general.name": ("string_declared", 2 * 1024 * 1024),
            },
        )

        facts = read_gguf(path)

        self.assertEqual(facts.architecture, "qwen3")
        self.assertEqual(facts.raw["error"], "string_too_large")

    def test_total_budget_is_enforced(self) -> None:
        path = make_gguf(self.tmp, self.FULL_KV)

        # 把 16 MiB 预算压到 32 字节：头部 24 字节后连第一个 key 都不够读。
        with mock.patch("backend.registry.discovery.facts._MAX_GGUF_BYTES", 32):
            facts = read_gguf(path)

        self.assertIsNone(facts.architecture)
        self.assertEqual(facts.raw["error"], "budget_exceeded")

    def test_vision_from_mmproj_sibling_and_flag(self) -> None:
        path = make_gguf(self.tmp, self.FULL_KV)
        (self.tmp / "mmproj-vision.gguf").write_bytes(b"")

        detected = read_gguf(path)
        self.assertTrue(detected.vision)
        self.assertEqual(detected.task, "vision")

        # 明确传 vision=True 时不需要 mmproj 文件。
        other = self.tmp / "plain"
        other.mkdir()
        plain = make_gguf(other, self.FULL_KV)
        forced = read_gguf(plain, vision=True)
        self.assertTrue(forced.vision)
        self.assertEqual(forced.task, "vision")


class HfFixtureTest(_TempDirTest):
    def test_normal_config(self) -> None:
        path = self.write_json(
            "config.json",
            {
                "architectures": ["Qwen2ForCausalLM"],
                "model_type": "qwen2",
                "max_position_embeddings": 32768,
                "num_hidden_layers": 28,
                "hidden_size": 3584,
                "num_attention_heads": 28,
                "num_key_value_heads": 4,
                "torch_dtype": "bfloat16",
            },
        )

        facts = read_hf(path)

        self.assertEqual(facts.format, "hf")
        self.assertEqual(facts.architecture, "Qwen2ForCausalLM")
        self.assertEqual(facts.file_type, "bfloat16")
        self.assertEqual(facts.context_length, 32768)
        self.assertEqual(facts.block_count, 28)
        self.assertEqual(facts.embedding_length, 3584)
        self.assertEqual(facts.head_count, 28)
        self.assertEqual(facts.head_count_kv, 4)
        self.assertIsNone(facts.expert_count)
        self.assertFalse(facts.vision)
        self.assertEqual(facts.task, "chat")
        self.assertIn("Qwen2ForCausalLM", facts.raw["architectures"])

    def test_missing_fields(self) -> None:
        path = self.write_json("config.json", {})

        facts = read_hf(path)

        self.assertIsNone(facts.architecture)
        self.assertIsNone(facts.file_type)
        self.assertIsNone(facts.context_length)
        self.assertIsNone(facts.block_count)
        self.assertEqual(facts.task, "unknown")
        self.assertFalse(facts.vision)

    def test_quantization_config_overrides_dtype(self) -> None:
        path = self.write_json(
            "config.json",
            {
                "architectures": ["Qwen2ForCausalLM"],
                "torch_dtype": "float16",
                "quantization_config": {"quant_method": "gptq", "bits": 4},
            },
        )

        self.assertEqual(read_hf(path).file_type, "gptq-4bit")

    def test_quantization_config_without_bits(self) -> None:
        path = self.write_json(
            "config.json",
            {"quantization_config": {"quant_method": "awq"}},
        )

        self.assertEqual(read_hf(path).file_type, "awq")

    def test_text_config_and_rope_fallbacks(self) -> None:
        text_dir = self.tmp / "text"
        text_dir.mkdir()
        text_path = self.write_json(
            "config.json",
            {"architectures": ["XForCausalLM"], "text_config": {"max_position_embeddings": 8192}},
            directory=text_dir,
        )
        self.assertEqual(read_hf(text_path).context_length, 8192)

        rope_path = self.write_json(
            "rope_config.json",
            {
                "architectures": ["XForCausalLM"],
                "rope_scaling": {"original_max_position_embeddings": 16384},
            },
        )
        self.assertEqual(read_hf(rope_path).context_length, 16384)

    def test_vision_language_config(self) -> None:
        path = self.write_json(
            "config.json",
            {
                "architectures": ["Qwen2VLForConditionalGeneration"],
                "model_type": "qwen2_vl",
                "vision_config": {"depth": 24},
                "text_config": {
                    "max_position_embeddings": 32768,
                    "num_hidden_layers": 28,
                    "hidden_size": 3584,
                    "num_attention_heads": 28,
                    "num_key_value_heads": 4,
                },
            },
        )

        facts = read_hf(path)

        self.assertTrue(facts.vision)
        self.assertEqual(facts.task, "vision")
        self.assertEqual(facts.context_length, 32768)
        self.assertEqual(facts.block_count, 28)
        self.assertEqual(facts.embedding_length, 3584)
        self.assertEqual(facts.head_count_kv, 4)

    def test_vision_flag_forces_vision(self) -> None:
        path = self.write_json("config.json", {"architectures": ["Qwen2ForCausalLM"]})

        facts = read_hf(path, vision=True)

        self.assertTrue(facts.vision)
        self.assertEqual(facts.task, "vision")

    def test_sentence_transformers_marker(self) -> None:
        self.write_json(
            "config.json",
            {
                "architectures": ["BertModel"],
                "model_type": "bert",
                "max_position_embeddings": 512,
            },
        )
        self.write_json("modules.json", [])

        facts = read_hf(self.tmp / "config.json")

        self.assertEqual(facts.task, "embedding")
        self.assertFalse(facts.vision)

    def test_sentence_transformers_alt_marker(self) -> None:
        self.write_json("config.json", {"architectures": ["BertModel"]})
        self.write_json("config_sentence_transformers.json", {})

        self.assertEqual(read_hf(self.tmp / "config.json").task, "embedding")

    def test_sequence_classification(self) -> None:
        path = self.write_json(
            "config.json", {"architectures": ["Qwen3ForSequenceClassification"]}
        )

        self.assertEqual(read_hf(path).task, "decision")

    def test_bad_json(self) -> None:
        path = self.tmp / "config.json"
        path.write_text("{ this is not json", encoding="utf-8")

        facts = read_hf(path)

        self.assertEqual(facts.raw["error"], "bad_json")
        self.assertEqual(facts.task, "unknown")
        self.assertIsNone(facts.architecture)

    def test_missing_config(self) -> None:
        facts = read_hf(self.tmp / "config.json")

        self.assertEqual(facts.raw["error"], "missing")
        self.assertEqual(facts.task, "unknown")

    def test_config_too_large(self) -> None:
        path = self.tmp / "config.json"
        # 合法 JSON，但超过 256 KB；实现必须在解析前放弃。
        path.write_text('{"x": "' + "a" * (300 * 1024) + '"}', encoding="utf-8")

        facts = read_hf(path)

        self.assertEqual(facts.raw["error"], "config_too_large")
        self.assertIsNone(facts.architecture)


class DiffusersFixtureTest(_TempDirTest):
    def test_image_pipeline(self) -> None:
        path = self.write_json(
            "model_index.json",
            {"_class_name": "StableDiffusionXLPipeline", "_diffusers_version": "0.30"},
        )

        facts = read_diffusers(path)

        self.assertEqual(facts.format, "diffusers")
        self.assertEqual(facts.task, "image")
        self.assertIsNone(facts.context_length)
        self.assertEqual(facts.raw["_class_name"], "StableDiffusionXLPipeline")

    def test_video_pipeline(self) -> None:
        for class_name in ("CogVideoXPipeline", "AnimateDiffPipeline", "WanPipeline"):
            with self.subTest(class_name=class_name):
                path = self.write_json("model_index.json", {"_class_name": class_name})
                facts = read_diffusers(path)
                self.assertEqual(facts.task, "video")
                self.assertEqual(facts.raw["_class_name"], class_name)

    def test_class_name_array(self) -> None:
        path = self.write_json(
            "model_index.json", {"_class_name": [None, "", "FluxPipeline"]}
        )

        facts = read_diffusers(path)

        self.assertEqual(facts.task, "image")
        self.assertEqual(facts.raw["_class_name"], "FluxPipeline")

    def test_missing_class_name_is_unknown(self) -> None:
        path = self.write_json("model_index.json", {"_diffusers_version": "0.30"})

        facts = read_diffusers(path)

        self.assertEqual(facts.task, "unknown")
        self.assertEqual(facts.raw["error"], "no_class_name")

    def test_bad_json(self) -> None:
        path = self.tmp / "model_index.json"
        path.write_text("[not a mapping]", encoding="utf-8")

        facts = read_diffusers(path)

        self.assertEqual(facts.task, "unknown")
        self.assertEqual(facts.raw["error"], "bad_json")


def _candidate(path: Path, kind: str, **overrides) -> ModelCandidate:
    values = {
        "path": Path(path),
        "kind": kind,
        "size_bytes": 1,
        "device": 1,
        "inode": 1,
        "mtime_ns": 1,
    }
    values.update(overrides)
    return ModelCandidate(**values)


class ReadModelDispatchTest(_TempDirTest):
    def test_dispatch_gguf(self) -> None:
        path = make_gguf(
            self.tmp,
            {
                "general.architecture": "qwen3",
                "qwen3.context_length": ("uint32", 4096),
            },
        )

        facts = read_model(_candidate(path, "gguf"))

        self.assertEqual(facts.format, "gguf")
        self.assertEqual(facts.architecture, "qwen3")
        self.assertEqual(facts.context_length, 4096)

    def test_dispatch_gguf_with_mmproj(self) -> None:
        model_dir = self.tmp / "gguf"
        model_dir.mkdir()
        model = make_gguf(model_dir, {"general.architecture": "qwen3"})
        mmproj = model_dir / "mmproj-model.gguf"
        mmproj.write_bytes(b"")

        facts = read_model(_candidate(model, "gguf", mmproj=mmproj))

        self.assertTrue(facts.vision)
        self.assertEqual(facts.task, "vision")

    def test_dispatch_hf_uses_default_config(self) -> None:
        self.write_json("config.json", {"architectures": ["Qwen2ForCausalLM"]})

        facts = read_model(_candidate(self.tmp, "hf"))

        self.assertEqual(facts.format, "hf")
        self.assertEqual(facts.architecture, "Qwen2ForCausalLM")
        self.assertEqual(facts.task, "chat")

    def test_dispatch_hf_uses_explicit_config(self) -> None:
        config = self.write_json(
            "snapshots/abc/config.json", {"architectures": ["BertForSequenceClassification"]}
        )

        facts = read_model(_candidate(self.tmp, "hf", config=config))

        self.assertEqual(facts.format, "hf")
        self.assertEqual(facts.task, "decision")

    def test_dispatch_diffusers(self) -> None:
        self.write_json("model_index.json", {"_class_name": "FluxPipeline"})

        facts = read_model(_candidate(self.tmp, "diffusers"))

        self.assertEqual(facts.format, "diffusers")
        self.assertEqual(facts.task, "image")

    def test_dispatch_diffusers_explicit_index(self) -> None:
        index = self.write_json(
            "elsewhere/model_index.json", {"_class_name": "CogVideoXPipeline"}
        )

        facts = read_model(_candidate(self.tmp, "diffusers", config=index))

        self.assertEqual(facts.task, "video")


class InferTaskOrderingTest(unittest.TestCase):
    def test_unknown_without_signal(self) -> None:
        self.assertEqual(infer_task([]), "unknown")
        self.assertEqual(infer_task(["BertModel"]), "unknown")
        self.assertEqual(infer_task(["SomethingElse"]), "unknown")

    def test_chat(self) -> None:
        self.assertEqual(infer_task(["Qwen2ForCausalLM"]), "chat")

    def test_decision(self) -> None:
        self.assertEqual(infer_task(["Qwen3ForSequenceClassification"]), "decision")

    def test_vision_beats_chat(self) -> None:
        self.assertEqual(infer_task(["Qwen2VLForConditionalGeneration"], vision=True), "vision")
        self.assertEqual(infer_task(["LlavaForConditionalGeneration"]), "unknown")
        self.assertEqual(
            infer_task(["LlavaForConditionalGeneration"], vision=True), "vision"
        )
        self.assertEqual(infer_task(["XForCausalLM"], has_mmproj=True), "vision")

    def test_diffusers_wins_first(self) -> None:
        self.assertEqual(
            infer_task(["XForCausalLM"], diffusers_class="FluxPipeline"), "image"
        )
        self.assertEqual(
            infer_task(["XForCausalLM"], diffusers_class="CogVideoXPipeline"), "video"
        )

    def test_embedding_beats_chat(self) -> None:
        self.assertEqual(
            infer_task(["XForCausalLM"], extra_config={"embedding": True}), "embedding"
        )
        self.assertEqual(
            infer_task(
                ["BertModel"], extra_config={"model_type": "sentence-transformers"}
            ),
            "embedding",
        )

    def test_decision_beats_chat(self) -> None:
        self.assertEqual(
            infer_task(["WeirdForSequenceClassificationForCausalLM"]), "decision"
        )


if __name__ == "__main__":
    unittest.main()
