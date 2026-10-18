"""评测类型、允许清单里的数据集、参数范围、以及安全预设。

这是「一次评测请求可以说什么」的**唯一**真源。它的立场是封闭的：字段不在清单里就是
拒绝，值不在范围内就是拒绝，预设不允许附加参数就是拒绝。

为什么要这么严（以及为什么拒绝而不是忽略）
------------------------------------------
一个评测请求最终要变成「某个 runner 去做某事」。如果请求里能自由携带命令行、脚本路径、
外部地址，那么这个接口立刻变成一台**在服务器上以服务身份执行任意命令**的机器，
而它前面只有一层薄薄的 Web 鉴权。所以三类东西被**结构性**地分开：

* **允许清单** —— ``dataset_slug`` 必须精确命中 :data:`DATASET_CATALOG` 里的某个键，
  参数必须是该数据集声明的参数，且落在声明范围内。这是唯一"能自由填"的部分。
* **安全预设** —— 一个名字对应一整套固定的参数捆绑。预设**不接受**任何附加参数：
  能覆盖预设参数的"预设"不是预设，而是一个带好听名字的自由表单。
* **禁止字段** —— ``command`` / ``args`` / ``path`` / ``url`` 之类一律**拒绝整个请求**，
  而不是静默丢弃。静默丢弃会让一次越界尝试在日志里消失，也让调用方以为自己写对了。
  这与 ``backend.registry.identity`` 对自报身份字段的处理是同一个立场。

本构建里所有数据集都是 ``synthetic_fixture``
--------------------------------------------
``DATASET_CATALOG`` 里的条目是**平台自己的允许清单**，不是 EvalScope 的真实数据集名。
真实的 "EvalScope 数据集 slug -> 本平台 slug" 映射**尚未接线**（需要真实的 EvalScope
版本与能力探测），所以此刻任何真实基准名都不会命中允许清单——这正是 fail closed 想要的结果：
没有映射就是「不在允许清单里」，而不是「猜一个最像的」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

from .errors import InvalidRequest

# ---------------------------------------------------------------------------
# 评测类型
# ---------------------------------------------------------------------------

#: 单次调用即可完成的正确性评测（不压 GPU）。
EVAL_TYPE_CORRECTNESS = "correctness"

#: 吞吐评测：**会压 GPU**（并发打满）。
EVAL_TYPE_THROUGHPUT = "throughput"

#: 时延评测：**会压 GPU**（重复压测以取分位数）。
EVAL_TYPE_LATENCY = "latency"

#: EvalScope 安全预设：由允许清单里的预设名驱动，参数不可自由填。
EVAL_TYPE_SAFETY_PRESET = "safety_preset"

EVAL_TYPES = (
    EVAL_TYPE_CORRECTNESS,
    EVAL_TYPE_THROUGHPUT,
    EVAL_TYPE_LATENCY,
    EVAL_TYPE_SAFETY_PRESET,
)

#: 会占用 GPU 的评测类型。对**他人共享**的目标默认拒绝这些类型。
GPU_STRESSING_EVAL_TYPES = frozenset({EVAL_TYPE_THROUGHPUT, EVAL_TYPE_LATENCY})

# 参数取值域
PARAM_INT = "int"
PARAM_FLOAT = "float"
PARAM_BOOL = "bool"
PARAM_CHOICE = "choice"
PARAM_KINDS = (PARAM_INT, PARAM_FLOAT, PARAM_BOOL, PARAM_CHOICE)

#: 一个数据集最多声明多少参数；请求里最多出现多少参数。
MAX_PARAMS = 16

#: 单个字符串参数的最大长度（仅 choice 会用到，留一个上界防止参数被当成载荷）。
MAX_STRING_PARAM = 64

#: 任何请求里**永远不能**出现的字段。命中即拒绝整个请求。
#:
#: 分四类，每一类都对应一条真实的攻击面：
#: 执行（command/args/entrypoint/env/cwd）、文件系统（path/file/dataset_path/image）、
#: 网络（url/uri/host/port/endpoint）、以及正文数据（prompt/content/messages）。
FORBIDDEN_SPEC_FIELDS = frozenset(
    {
        # 执行面
        "command",
        "cmd",
        "argv",
        "args",
        "shell",
        "sh",
        "script",
        "entrypoint",
        "executable",
        "python",
        "module",
        "code",
        "env",
        "environment",
        "cwd",
        "workdir",
        "sudo",
        "user",
        "uid",
        # 文件系统面
        "path",
        "file",
        "files",
        "filename",
        "filepath",
        "dataset_path",
        "data_path",
        "weights",
        "weight_path",
        "model_path",
        "image",
        "images",
        "mount",
        "volume",
        # 网络面
        "url",
        "uri",
        "address",
        "addr",
        "host",
        "hostname",
        "port",
        "endpoint",
        "endpoint_url",
        "base_url",
        "proxy",
        "callback",
        "webhook",
        # 正文数据面
        "prompt",
        "prompts",
        "completion",
        "completions",
        "content",
        "messages",
        "text",
        "raw",
        "payload",
        "config",
        "yaml",
        "toml",
        # 授权面（身份同样不能自报，见 identity.py）
        "owner",
        "principal",
        "project",
        "visibility",
        "scopes",
        "role",
        "is_admin",
    }
)


@dataclass(frozen=True)
class ParamSpec:
    """一个数据集参数的声明：类型 + 范围 + 缺省。

    ``required`` 为真时请求必须给出该参数。范围只对 ``int`` / ``float`` 有意义；
    ``choice`` 用 ``choices``。``bool`` 参数**不接受** ``0/1/"true"``：一个能被两种
    类型写出来的参数，会让「服务端到底收到了什么」变成一个需要猜的问题。
    """

    name: str
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    default: object = None
    required: bool = False
    unit: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in PARAM_KINDS:
            raise InvalidRequest(f"unknown parameter kind: {self.kind!r}")
        if self.kind == PARAM_CHOICE and not self.choices:
            raise InvalidRequest(f"choice parameter {self.name!r} declares no choices")
        if self.kind != PARAM_CHOICE and self.choices:
            raise InvalidRequest(f"only choice parameters may declare choices: {self.name!r}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise InvalidRequest(f"parameter {self.name!r} has an empty range")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices),
            "default": self.default,
            "required": self.required,
            "unit": self.unit,
            "note": self.note,
        }

    def validate(self, raw: object, *, dataset_slug: str) -> object:
        """校验一个取值，返回规范化后的 Python 值。"""
        where = f"{dataset_slug}.{self.name}"

        if raw is None:
            if self.required:
                raise InvalidRequest(f"{where} is required")
            return self.default

        if self.kind == PARAM_BOOL:
            if not isinstance(raw, bool):
                raise InvalidRequest(f"{where} must be a JSON boolean")
            return raw

        if self.kind == PARAM_CHOICE:
            if not isinstance(raw, str):
                raise InvalidRequest(f"{where} must be one of {list(self.choices)}")
            if raw not in self.choices:
                raise InvalidRequest(f"{where} must be one of {list(self.choices)}")
            return raw

        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidRequest(f"{where} must be a number")
        if isinstance(raw, float) and not math.isfinite(raw):
            # 裸 NaN / Infinity 不是合法 JSON；到这里的是 Python 侧的调用方。
            raise InvalidRequest(f"{where} must be finite")

        if self.kind == PARAM_INT:
            if isinstance(raw, float):
                if not float(raw).is_integer():
                    raise InvalidRequest(f"{where} must be an integer")
                raw = int(raw)
            value: float = int(raw)
        else:
            value = float(raw)

        if self.minimum is not None and value < self.minimum:
            raise InvalidRequest(f"{where} must be >= {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise InvalidRequest(f"{where} must be <= {self.maximum}")
        return int(value) if self.kind == PARAM_INT else value


@dataclass(frozen=True)
class DatasetSpec:
    """允许清单里的一个数据集。"""

    slug: str
    version: str
    display_name: str
    description: str
    eval_types: tuple[str, ...]
    params: tuple[ParamSpec, ...] = ()
    synthetic_fixture: bool = True

    def param(self, name: str) -> ParamSpec | None:
        for spec in self.params:
            if spec.name == name:
                return spec
        return None

    def validate_params(self, raw: object) -> dict:
        """把请求参数压成一份**完整、规范化**的参数字典。

        未被声明的参数是错误，不是噪声。缺省的参数会被补上缺省值，因此最终落库的
        参数字典是自洽的、可复现的（每个键都来自本数据集的声明）。
        """
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise InvalidRequest("params must be a JSON object")

        if len(raw) > MAX_PARAMS:
            raise InvalidRequest(f"at most {MAX_PARAMS} parameters are accepted")

        declared = {spec.name for spec in self.params}
        unknown = sorted(set(raw) - declared)
        if unknown:
            raise InvalidRequest(
                f"dataset {self.slug!r} does not accept parameter(s): {', '.join(unknown)}"
            )

        normalized: dict = {}
        for spec in self.params:
            normalized[spec.name] = spec.validate(raw.get(spec.name), dataset_slug=self.slug)
        return normalized

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "eval_types": list(self.eval_types),
            "params": [spec.to_dict() for spec in self.params],
            "synthetic_fixture": self.synthetic_fixture,
        }


#: 允许清单。**这是全部**：不在里面的 slug 一律拒绝。
DATASET_CATALOG: Mapping[str, DatasetSpec] = {
    "synthetic-qa-smoke": DatasetSpec(
        slug="synthetic-qa-smoke",
        version="1",
        display_name="合成问答冒烟集",
        description=(
            "平台自有的极小型合成问答样本，用于验证评测链路本身（提交/排队/结果形状）。"
            "不含任何真实数据，也不对应 EvalScope 的真实基准。"
        ),
        eval_types=(EVAL_TYPE_CORRECTNESS, EVAL_TYPE_LATENCY),
        params=(
            ParamSpec("limit", PARAM_INT, minimum=1, maximum=200, default=8, unit="samples"),
            ParamSpec("few_shot", PARAM_INT, minimum=0, maximum=5, default=0),
            ParamSpec(
                "seed",
                PARAM_INT,
                minimum=0,
                maximum=2_147_483_647,
                default=0,
                note="固定种子让合成样本可复现。",
            ),
            ParamSpec("temperature", PARAM_FLOAT, minimum=0.0, maximum=1.0, default=0.0),
        ),
    ),
    "synthetic-gen-smoke": DatasetSpec(
        slug="synthetic-gen-smoke",
        version="1",
        display_name="合成生成冒烟集",
        description=(
            "平台自有的合成生成样本，用于吞吐/时延链路的形状验证。"
            "并发上限刻意压得很低：本部署的 GPU 已满，任何真实压测都不应被顺手开出来。"
        ),
        eval_types=(EVAL_TYPE_THROUGHPUT, EVAL_TYPE_LATENCY),
        params=(
            ParamSpec("limit", PARAM_INT, minimum=1, maximum=200, default=8, unit="samples"),
            ParamSpec("concurrency", PARAM_INT, minimum=1, maximum=4, default=1),
            ParamSpec("max_tokens", PARAM_INT, minimum=1, maximum=2048, default=64),
            ParamSpec("timeout_s", PARAM_INT, minimum=1, maximum=600, default=60, unit="s"),
        ),
    ),
    "synthetic-safety-smoke": DatasetSpec(
        slug="synthetic-safety-smoke",
        version="1",
        display_name="合成安全冒烟集",
        description=(
            "平台自有的合成安全样本。真实的 EvalScope 安全数据集映射尚未接线；"
            "此条目只用于验证安全预设的形状与授权路径。"
        ),
        eval_types=(EVAL_TYPE_SAFETY_PRESET,),
        params=(
            ParamSpec("limit", PARAM_INT, minimum=1, maximum=100, default=4, unit="samples"),
            ParamSpec(
                "category",
                PARAM_CHOICE,
                choices=("refusal", "pii", "toxicity"),
                default="refusal",
            ),
        ),
    ),
}


@dataclass(frozen=True)
class SafetyPreset:
    """一个 EvalScope 安全预设：**固定**数据集 + **固定**参数捆绑。

    ``params`` 是预设的一部分而不是缺省值：调用者不能覆盖它们。想改参数就必须
    提交一个显式的、允许清单里的数据集任务，而不是"改一改预设"。
    """

    name: str
    dataset_slug: str
    params: tuple[tuple[str, object], ...]
    description: str

    def resolved_params(self) -> dict:
        return {key: value for key, value in self.params}


#: 安全预设允许清单。
SAFETY_PRESETS: Mapping[str, SafetyPreset] = {
    "safety-refusal-sample": SafetyPreset(
        name="safety-refusal-sample",
        dataset_slug="synthetic-safety-smoke",
        params=(("limit", 4), ("category", "refusal")),
        description="合成拒答样本：验证安全评测的授权与报告形状，不做真实安全判定。",
    ),
    "safety-pii-sample": SafetyPreset(
        name="safety-pii-sample",
        dataset_slug="synthetic-safety-smoke",
        params=(("limit", 4), ("category", "pii")),
        description="合成 PII 样本：同上，仅形状验证。",
    ),
}


@dataclass(frozen=True)
class EvalSpec:
    """一次评测请求被校验、规范化之后的形状。

    这是**唯一**能进入 service 层的请求形状：到达这里之前，所有自由文本、所有禁止
    字段、所有越界参数都已经变成了拒绝。
    """

    eval_type: str
    dataset_slug: str
    dataset_version: str
    params: Mapping[str, object]
    preset: str | None = None

    @property
    def gpu_stressing_eval_type(self) -> bool:
        return self.eval_type in GPU_STRESSING_EVAL_TYPES

    def to_dict(self) -> dict:
        return {
            "eval_type": self.eval_type,
            "dataset_slug": self.dataset_slug,
            "dataset_version": self.dataset_version,
            "params": dict(self.params),
            "preset": self.preset,
        }


def reject_forbidden_spec_fields(raw: object) -> None:
    """请求里出现任何禁止字段，抛 :class:`InvalidRequest`（拒绝整个请求）。

    只看顶层键。嵌套对象不在这里递归——因为**没有任何**嵌套结构是本契约的一部分：
    参数只能是标量，所以一个嵌套对象本身已经是未知参数，会在参数级被拒。
    """
    if not isinstance(raw, Mapping):
        return
    offending = sorted(set(raw) & FORBIDDEN_SPEC_FIELDS)
    if offending:
        raise InvalidRequest(
            "the request may not carry executable, filesystem, network or content "
            f"fields; remove: {', '.join(offending)}"
        )


def validate_eval_type(raw: object) -> str:
    if not isinstance(raw, str) or raw not in EVAL_TYPES:
        raise InvalidRequest(f"eval_type must be one of {list(EVAL_TYPES)}")
    return raw


def validate_preset(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in SAFETY_PRESETS:
        raise InvalidRequest(f"unknown safety preset: {raw!r}")
    return raw


def build_eval_spec(
    *,
    eval_type: object,
    dataset_slug: object = None,
    params: object = None,
    preset: object = None,
) -> EvalSpec:
    """把一次「想跑什么」的请求压成 :class:`EvalSpec`，或拒绝它。

    两条路互斥且都必须完整：

    * ``eval_type == safety_preset`` —— 必须给 ``preset``，**不得**给 ``dataset_slug``
      或 ``params``。预设自带数据集与参数。
    * 其余类型 —— 必须给 ``dataset_slug``，``params`` 必须落在该数据集的声明范围内，
      且该类型必须在数据集的 ``eval_types`` 里。
    """
    resolved_type = validate_eval_type(eval_type)

    if resolved_type == EVAL_TYPE_SAFETY_PRESET:
        name = validate_preset(preset)
        if name is None:
            raise InvalidRequest("eval_type safety_preset requires a preset")
        if dataset_slug is not None:
            raise InvalidRequest("a safety preset supplies its own dataset; do not send dataset_slug")
        if params not in (None, {}):
            raise InvalidRequest("a safety preset is fixed; it accepts no params")
        chosen = SAFETY_PRESETS[name]
        dataset = DATASET_CATALOG[chosen.dataset_slug]
        return EvalSpec(
            eval_type=resolved_type,
            dataset_slug=dataset.slug,
            dataset_version=dataset.version,
            params=dataset.validate_params(chosen.resolved_params()),
            preset=name,
        )

    if preset is not None:
        raise InvalidRequest("preset is only valid when eval_type is safety_preset")

    if not isinstance(dataset_slug, str) or not dataset_slug:
        raise InvalidRequest("dataset_slug is required")
    dataset = DATASET_CATALOG.get(dataset_slug)
    if dataset is None:
        # 刻意不回显"最接近的那个"：那会把允许清单变成一个可枚举的预言机。
        raise InvalidRequest(
            f"dataset {dataset_slug!r} is not on the allowlist; "
            f"known slugs: {sorted(DATASET_CATALOG)}"
        )
    if resolved_type not in dataset.eval_types:
        raise InvalidRequest(
            f"dataset {dataset.slug!r} does not support eval_type {resolved_type!r}; "
            f"it supports {list(dataset.eval_types)}"
        )

    return EvalSpec(
        eval_type=resolved_type,
        dataset_slug=dataset.slug,
        dataset_version=dataset.version,
        params=dataset.validate_params(params),
        preset=None,
    )


def catalog_dto() -> dict:
    """允许清单的可展示视图（给 API / CLI / UI 用）。"""
    return {
        "eval_types": list(EVAL_TYPES),
        "gpu_stressing_eval_types": sorted(GPU_STRESSING_EVAL_TYPES),
        "datasets": [DATASET_CATALOG[slug].to_dict() for slug in sorted(DATASET_CATALOG)],
        "safety_presets": [
            {
                "name": SAFETY_PRESETS[name].name,
                "dataset_slug": SAFETY_PRESETS[name].dataset_slug,
                "params": SAFETY_PRESETS[name].resolved_params(),
                "description": SAFETY_PRESETS[name].description,
            }
            for name in sorted(SAFETY_PRESETS)
        ],
        "forbidden_request_fields": sorted(FORBIDDEN_SPEC_FIELDS),
        "note": (
            "允许清单是**平台自有**的合成条目；EvalScope 真实数据集名到本清单的映射尚未接线，"
            "因此真实基准名不会命中。"
        ),
    }


def sorted_dataset_slugs() -> list[str]:
    return sorted(DATASET_CATALOG)


def iter_params(specs: Iterable[ParamSpec]) -> list[str]:
    return [spec.name for spec in specs]


__all__ = [
    "DATASET_CATALOG",
    "EVAL_TYPES",
    "EVAL_TYPE_CORRECTNESS",
    "EVAL_TYPE_LATENCY",
    "EVAL_TYPE_SAFETY_PRESET",
    "EVAL_TYPE_THROUGHPUT",
    "FORBIDDEN_SPEC_FIELDS",
    "GPU_STRESSING_EVAL_TYPES",
    "MAX_PARAMS",
    "MAX_STRING_PARAM",
    "PARAM_BOOL",
    "PARAM_CHOICE",
    "PARAM_FLOAT",
    "PARAM_INT",
    "PARAM_KINDS",
    "SAFETY_PRESETS",
    "DatasetSpec",
    "EvalSpec",
    "ParamSpec",
    "SafetyPreset",
    "build_eval_spec",
    "catalog_dto",
    "iter_params",
    "reject_forbidden_spec_fields",
    "sorted_dataset_slugs",
    "validate_eval_type",
    "validate_preset",
]
