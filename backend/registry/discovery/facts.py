"""§5.4 的模型元数据（facts）提取。

约定：**坏文件不是异常**。截断、magic 不对、KV 类型未知、坏 JSON、超大数组声明
都会降级成「已经解析到的部分 + ``raw["error"]``」，因为扫描器面对的是用户目录，
一个坏文件不能让整轮 sync 失败。调用方只看 ``ModelFacts``，不需要 try/except。

GGUF 只解析头部 KV 区（``kv_count`` 条），绝不读取张量数据；HF 的 config.json 和
diffusers 的 model_index.json 都是纯 JSON。因此这里不依赖 torch，也不会对
``.pt`` / pickle 做反序列化（§5.4 明令禁止）。

资源上限（16 MiB / 单字符串 1 MiB / 数组 1e6 元素）都按「读到多少算多少」实现：
一旦越界就停在当前位置，保留此前解析成功的键。这样既能防御恶意/损坏文件，也能
容忍超大词表单。
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path

from backend.registry.discovery.types import ModelCandidate, ModelFacts

__all__ = ["read_model", "read_gguf", "read_hf", "read_diffusers", "infer_task"]

#: §5.4：GGUF KV 区最多读 16 MiB，够覆盖常见模型的全部元数据。
_MAX_GGUF_BYTES = 16 * 1024 * 1024
#: 单个字符串（键或值）的上限。超出多半是文件损坏或恶意长度。
_MAX_STRING_BYTES = 1024 * 1024
#: 声明元素超过一百万的数组不再相信；先停下，保留已解析的键。
_MAX_ARRAY_ELEMENTS = 1_000_000
#: 数组只留前若干个用于调试；string 数组虽然完整解析，但 raw 里也只展示这些。
_ARRAY_PREVIEW = 16
#: §5.4：HF config.json 超过 256 KB 直接放弃，正常 config 远小于此。
_MAX_JSON_BYTES = 256 * 1024

_GGUF_MAGIC = b"GGUF"
_GGUF_VERSIONS = (2, 3)

# GGUF / ggml 的标量类型（type 8/9 单独处理）。
_SCALAR_FORMATS: Mapping[int, str] = {
    0: "<B",  # uint8
    1: "<b",  # int8
    2: "<H",  # uint16
    3: "<h",  # int16
    4: "<I",  # uint32
    5: "<i",  # int32
    6: "<f",  # float32
    7: "<?",  # bool（GGUF 里是 1 字节）
    10: "<Q",  # uint64
    11: "<q",  # int64
    12: "<d",  # float64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9

#: llama.cpp 的 ``general.file_type`` 枚举（与 ggml_type 不是一回事）。
#: 4/5/6 是已废弃的值，故意不映射，落到 ``str(int)``。
_GGUF_FILE_TYPES: Mapping[int, str] = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    7: "Q8_0",
    8: "Q5_0",
    9: "Q5_1",
    10: "Q2_K",
    11: "Q3_K_S",
    12: "Q3_K_M",
    13: "Q3_K_L",
    14: "Q4_K_S",
    15: "Q4_K_M",
    16: "Q5_K_S",
    17: "Q5_K_M",
    18: "Q6_K",
    19: "IQ2_XXS",
    20: "IQ2_XS",
    21: "IQ3_XXS",
    22: "IQ1_S",
    23: "IQ4_NL",
    24: "IQ3_S",
    25: "IQ2_S",
    26: "IQ4_XS",
    27: "IQ1_M",
    28: "BF16",
}

#: diffusers 里决定 task=video 的类名片段；其余 diffusers 一律 image。
_VIDEO_CLASS_MARKERS = ("video", "cogvideox", "animatediff", "wan")

#: §5.4 的架构后缀。只认这些明确后缀，避免把别的 *ForCausalLM 之外的东西猜成 chat。
_DECISION_MARKER = "ForSequenceClassification"
_CHAT_MARKER = "ForCausalLM"

#: 视觉模型的命名很杂：Vision/LLaVA/Qwen2-VL/InternVL 等。宁可少认，不要错认。
_VISION_PATTERN = re.compile(r"(vision|llava|internvl|qwen[\d.]*[-_]?vl|_vl\b|[-_]vl\b)")


class _StopParsing(Exception):
    """内部信号：再读下去要么越界、要么数据不可信。

    消息直接写进 ``raw["error"]``，所以保持短小的英文标识，便于排查和统计。
    """


class _BudgetReader:
    """带总字节预算的顺序读取器。

    用预算而不是 ``read(16 MiB)`` 再切片，是因为 GGUF 可能有几十 GB；这里只读
    元数据区，绝不能把张量数据拉进内存。
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self.remaining = _MAX_GGUF_BYTES

    def read(self, size: int) -> bytes:
        if size < 0:
            raise _StopParsing("truncated")
        if size > self.remaining:
            # 预算比文件先耗尽：当作截断处理，保留已解析的键。
            raise _StopParsing("budget_exceeded")
        data = self._stream.read(size)
        if len(data) != size:
            raise _StopParsing("truncated")
        self.remaining -= size
        return data

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]


def _read_gguf_string(reader: _BudgetReader) -> str:
    length = reader.unpack("<Q")
    if length > _MAX_STRING_BYTES:
        raise _StopParsing("string_too_large")
    # 词表里偶有非 UTF-8 字节；替换而不是报错，元数据不至于因此全丢。
    return reader.read(length).decode("utf-8", errors="replace")


def _read_gguf_value(reader: _BudgetReader, value_type: int, depth: int = 0):
    fmt = _SCALAR_FORMATS.get(value_type)
    if fmt is not None:
        return reader.unpack(fmt)
    if value_type == _TYPE_STRING:
        return _read_gguf_string(reader)
    if value_type == _TYPE_ARRAY:
        if depth:
            # GGUF 规范里没有嵌套数组；遇到就说明文件不可信，且无法安全跳过。
            raise _StopParsing("nested_array")
        element_type = reader.unpack("<I")
        count = reader.unpack("<Q")
        if count > _MAX_ARRAY_ELEMENTS:
            raise _StopParsing("array_too_large")
        if element_type == _TYPE_ARRAY:
            raise _StopParsing("nested_array")
        if element_type not in _SCALAR_FORMATS and element_type != _TYPE_STRING:
            raise _StopParsing("unknown_type")
        preview = []
        for index in range(count):
            value = _read_gguf_value(reader, element_type, depth + 1)
            if index < _ARRAY_PREVIEW:
                preview.append(value)
        # 这里返回的是「预览」，而元素本身已经被完整消费，后续 KV 仍能继续读。
        return preview
    raise _StopParsing("unknown_type")


def _stringify(value) -> str:
    """raw 里的值统一成字符串，方便 JSON 上报和日志对比。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)[:400]
        except (TypeError, ValueError):
            return str(value)[:400]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        text = "[" + ", ".join(_stringify(item) for item in value) + "]"
        return text[:400]
    return str(value)


def _as_int(value) -> int | None:
    """把数值字段收敛成 int；数组取第一个可用的元素。

    SD 之类模型的 ``attention.head_count`` 是每层/每分辨率的数组。对默认使用
    场景取第一个是最稳妥的：求和会把「头数」变成无意义的层数和。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, list):
        for item in value:
            result = _as_int(item)
            if result is not None:
                return result
    return None


def _file_type_name(value) -> str | None:
    number = _as_int(value)
    if number is None:
        return None
    return _GGUF_FILE_TYPES.get(number, str(number))


def _has_mmproj_sibling(path: Path) -> bool:
    """同目录下的 ``mmproj*.gguf`` 是多模态投影，等价于 vision 信号。"""
    try:
        entries = path.parent.iterdir()
    except OSError:
        return False
    for entry in entries:
        name = entry.name.lower()
        if name.startswith("mmproj") and name.endswith(".gguf"):
            try:
                if entry.is_file():
                    return True
            except OSError:
                continue
    return False


def _gguf_facts(kv: Mapping[str, object], error: str | None, vision: bool) -> ModelFacts:
    architecture = kv.get("general.architecture")
    if not isinstance(architecture, str) or not architecture:
        architecture = None
    prefix = f"{architecture}." if architecture else ""

    def numeric(suffix: str) -> int | None:
        if not prefix:
            return None
        return _as_int(kv.get(prefix + suffix))

    name = kv.get("general.name")
    if not isinstance(name, str) or not name:
        name = None

    raw = {key: _stringify(value) for key, value in kv.items()}
    if error:
        raw["error"] = error

    task = infer_task(
        (),
        vision=vision,
        has_mmproj=vision,
        diffusers_class=None,
        extra_config={},
    )
    return ModelFacts(
        format="gguf",
        architecture=architecture,
        name=name,
        file_type=_file_type_name(kv.get("general.file_type")),
        context_length=numeric("context_length"),
        block_count=numeric("block_count"),
        embedding_length=numeric("embedding_length"),
        head_count=numeric("attention.head_count"),
        head_count_kv=numeric("attention.head_count_kv"),
        expert_count=numeric("expert_count"),
        task=task,
        vision=vision,
        raw=raw,
    )


def read_gguf(path: Path, *, vision: bool = False) -> ModelFacts:
    """读取 GGUF 头部 KV；任何损坏都返回 best-effort 结果。"""
    path = Path(path)
    vision = bool(vision) or _has_mmproj_sibling(path)
    kv: dict[str, object] = {}
    error: str | None = None
    try:
        with open(path, "rb") as stream:
            reader = _BudgetReader(stream)
            if reader.read(4) != _GGUF_MAGIC:
                error = "bad_magic"
            else:
                version = reader.unpack("<I")
                if version not in _GGUF_VERSIONS:
                    error = "unsupported_version"
                else:
                    reader.unpack("<Q")  # tensor_count：元数据阶段用不到
                    kv_count = reader.unpack("<Q")
                    for _ in range(kv_count):
                        key = _read_gguf_string(reader)
                        value_type = reader.unpack("<I")
                        kv[key] = _read_gguf_value(reader, value_type)
    except _StopParsing as exc:
        error = str(exc)
    except OSError:
        error = "unreadable"
    except Exception:  # 兜底：一个畸形文件不能中断整轮扫描
        error = "parse_error"
    return _gguf_facts(kv, error, vision)


def _lookup(config: Mapping, text_config: Mapping, key: str):
    value = config.get(key)
    if value is None:
        value = text_config.get(key)
    return value


def _context_length(config: Mapping, text_config: Mapping) -> int | None:
    value = _as_int(config.get("max_position_embeddings"))
    if value is None:
        value = _as_int(text_config.get("max_position_embeddings"))
    if value is not None:
        return value
    # 长上下文模型常把真实上限藏在 rope_scaling 里。
    for source in (config.get("rope_scaling"), text_config.get("rope_scaling")):
        if not isinstance(source, Mapping):
            continue
        for key in ("max_position_embeddings", "original_max_position_embeddings"):
            value = _as_int(source.get(key))
            if value is not None:
                return value
    return None


def _quant_file_type(value) -> str | None:
    """把 ``quantization_config`` 压成一个短标签，如 ``gptq-4bit``。"""
    if not isinstance(value, Mapping):
        return None
    method = value.get("quant_method") or value.get("quantization_method")
    bits = _as_int(value.get("bits")) or _as_int(value.get("w_bit"))
    if bits is None:
        if value.get("load_in_4bit") is True:
            bits = 4
        elif value.get("load_in_8bit") is True:
            bits = 8
    if method is not None:
        name = str(method).lower()
        return f"{name}-{bits}bit" if bits is not None else name
    return f"{bits}bit" if bits is not None else None


def _architectures(config: Mapping) -> list[str]:
    value = config.get("architectures")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _mentions_vision(text: str) -> bool:
    return bool(_VISION_PATTERN.search(text.lower()))


def _config_vision(config: Mapping, architectures: Sequence[str]) -> bool:
    if isinstance(config.get("vision_config"), Mapping):
        return True
    candidates = [config.get("model_type"), config.get("architecture")]
    candidates.extend(architectures)
    return any(isinstance(text, str) and _mentions_vision(text) for text in candidates)


def _has_embedding_marker(directory: Path) -> bool:
    """sentence-transformers 导出的目录会带这两个文件之一。"""
    for name in ("modules.json", "config_sentence_transformers.json"):
        try:
            if (directory / name).is_file():
                return True
        except OSError:
            continue
    return False


def _hf_facts(
    config: Mapping | None,
    error: str | None,
    vision: bool,
    *,
    embedding: bool = False,
) -> ModelFacts:
    if config is None:
        raw = {"error": error} if error else {}
        task = infer_task(
            (), vision=vision, has_mmproj=vision, diffusers_class=None, extra_config={}
        )
        return ModelFacts(format="hf", task=task, vision=vision, raw=raw)

    text_config = config.get("text_config")
    if not isinstance(text_config, Mapping):
        text_config = {}

    architectures = _architectures(config)
    architecture = architectures[0] if architectures else None

    quant = config.get("quantization_config")
    file_type = _quant_file_type(quant)
    if file_type is None:
        dtype = _lookup(config, text_config, "torch_dtype")
        if isinstance(dtype, str) and dtype:
            file_type = dtype

    vision = vision or _config_vision(config, architectures)
    raw: dict[str, str] = {}
    for key in (
        "model_type",
        "architectures",
        "max_position_embeddings",
        "num_hidden_layers",
        "hidden_size",
        "num_attention_heads",
        "num_key_value_heads",
        "torch_dtype",
        "quantization_config",
    ):
        if key in config:
            raw[key] = _stringify(config[key])
    if error:
        raw["error"] = error

    task = infer_task(
        architectures,
        vision=vision,
        has_mmproj=False,
        diffusers_class=None,
        extra_config={"embedding": embedding, "model_type": config.get("model_type")},
    )
    return ModelFacts(
        format="hf",
        architecture=architecture,
        file_type=file_type,
        context_length=_context_length(config, text_config),
        block_count=_as_int(_lookup(config, text_config, "num_hidden_layers")),
        embedding_length=_as_int(_lookup(config, text_config, "hidden_size")),
        head_count=_as_int(_lookup(config, text_config, "num_attention_heads")),
        head_count_kv=_as_int(_lookup(config, text_config, "num_key_value_heads")),
        # MoE 的专家数在不同 config 里叫 num_experts / num_local_experts，
        # 都是显式字段而不是猜测。
        expert_count=(
            _as_int(_lookup(config, text_config, "num_experts"))
            or _as_int(_lookup(config, text_config, "num_local_experts"))
        ),
        task=task,
        vision=vision,
        raw=raw,
    )


def _load_json(path: Path) -> tuple[Mapping | None, str | None]:
    """读取小型 JSON 对象。返回 (数据, 错误标识)，从不抛异常。"""
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return None, "config_too_large"
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    except ValueError:
        return None, "bad_json"
    except Exception:  # 例如递归过深的 JSON
        return None, "parse_error"
    if not isinstance(data, Mapping):
        return None, "bad_json"
    return data, None


def read_hf(config_path: Path, *, vision: bool = False) -> ModelFacts:
    """读取 HF ``config.json``；sentence-transformers 的标记文件决定 embedding。"""
    config_path = Path(config_path)
    config, error = _load_json(config_path)
    # 同目录的 modules.json / config_sentence_transformers.json 是强信号，
    # 即便 architectures 看起来像普通 BERT 也应该判成 embedding。
    embedding = _has_embedding_marker(config_path.parent)
    return _hf_facts(config, error, bool(vision), embedding=embedding)


def _diffusers_class(data: Mapping) -> str | None:
    value = data.get("_class_name")
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def read_diffusers(model_index_path: Path) -> ModelFacts:
    """读取 diffusers 的 ``model_index.json``，只用 ``_class_name`` 分 image/video。"""
    model_index_path = Path(model_index_path)
    data, error = _load_json(model_index_path)
    raw: dict[str, str] = {}
    if data is None:
        if error:
            raw["error"] = error
        return ModelFacts(format="diffusers", task="unknown", raw=raw)

    diffusers_class = _diffusers_class(data)
    if diffusers_class is None:
        raw["error"] = error or "no_class_name"
        return ModelFacts(format="diffusers", task="unknown", raw=raw)

    raw["_class_name"] = diffusers_class
    task = infer_task(
        (),
        vision=False,
        has_mmproj=False,
        diffusers_class=diffusers_class,
        extra_config={},
    )
    return ModelFacts(format="diffusers", task=task, raw=raw)


def _embedding_signal(architectures: Sequence[str], extra_config: Mapping) -> bool:
    if extra_config.get("embedding"):
        return True
    candidates: list[object] = list(architectures)
    candidates.extend(
        extra_config.get(key) for key in ("model_type", "pipeline_tag", "architecture")
    )
    for value in candidates:
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        if "sentencetransformer" in lowered or "sentence-transformer" in lowered:
            return True
        if lowered.strip() == "sentence_similarity":
            return True
    return False


def infer_task(
    architectures,
    *,
    vision: bool = False,
    has_mmproj: bool = False,
    diffusers_class: str | None = None,
    extra_config: Mapping | None = None,
) -> str:
    """按 §5.4 的顺序推断 task。

    顺序是有意为之：diffusers 的产物已经知道是图/视频；多模态投影比 language
    architecture 更具体；sentence-transformers 即使 architectures 里写着
    ``*ForCausalLM`` 也仍然是 embedding。拿不准就 ``unknown``，交给主人改。
    """
    if diffusers_class:
        name = str(diffusers_class).lower()
        return "video" if any(marker in name for marker in _VIDEO_CLASS_MARKERS) else "image"
    if has_mmproj or vision:
        return "vision"

    if isinstance(architectures, str):
        arch_list = [architectures]
    else:
        arch_list = [item for item in (architectures or ()) if isinstance(item, str)]
    extra = extra_config or {}

    if _embedding_signal(arch_list, extra):
        return "embedding"
    joined = " ".join(arch_list)
    if _DECISION_MARKER in joined:
        return "decision"
    if _CHAT_MARKER in joined:
        return "chat"
    return "unknown"


def read_model(candidate: ModelCandidate) -> ModelFacts:
    """按 candidate.kind 分派到对应的读取器。

    ``path`` 对 GGUF 是文件，对 HF / diffusers 是目录；``config`` 允许调用方
    直接指出元数据文件（HF hub 场景），缺省时按约定文件名在同一目录找。
    """
    if candidate.kind == "gguf":
        return read_gguf(candidate.path, vision=candidate.mmproj is not None)
    if candidate.kind == "hf":
        config_path = candidate.config or (Path(candidate.path) / "config.json")
        return read_hf(config_path, vision=candidate.mmproj is not None)
    if candidate.kind == "diffusers":
        config = candidate.config
        if config is not None and Path(config).name == "model_index.json":
            model_index = Path(config)
        else:
            model_index = Path(candidate.path) / "model_index.json"
        return read_diffusers(model_index)
    # 类型上不可达；返回 unknown 而不是抛异常，保持「扫描不中断」的约定。
    return ModelFacts(format=candidate.kind, raw={"error": "unsupported_kind"})
