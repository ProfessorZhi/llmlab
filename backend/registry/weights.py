"""模型权重路径：一个**受限的登记引用**，而不是一个被打开的文件。

平台简化设计的前提是「模型文件不用移动」，推论是 Registry 对权重路径只有三件事
可做：**存下来、展示给自己的 owner、按 owner 的意愿共享那个 Endpoint**。

因此本模块**只做字符串校验**，不做任何文件系统调用：

* 不 ``stat``（存在性、大小、mtime 都不问）；
* 不打开、不读取、不哈希内容；
* 不 ``glob`` / 不遍历目录；
* 不复制、不移动、不建软链。

这不是「暂时没做」—— 是设计边界。权重文件在 owner 的 HOME 里，Registry 进程
未必有权限，也不该有。模型**能不能被加载**是 engine 自己的事，Registry 记录的
只是「owner 声明它在那里」。

副作用（正是想要的）：远端用户调用一个共享 Endpoint 时，**不需要**读权限 ——
文件权限完全留在 owner 与 engine 之间。

同一模块还提供 :func:`redact_weight_path`：把路径展示给非 owner 时，只给出
**可核对的指纹**而不给出目录结构。原因是 ``/home/<user>/...`` 本身就泄漏了
「这台机器上有哪些用户、谁在跑什么」，而共享的是服务，不是 owner 的家目录。
"""

from __future__ import annotations

import hashlib
import re

from .errors import InvalidRequest

#: 路径长度上限。远超正常模型目录深度，只是防止把整段文本塞进一个字段。
MAX_WEIGHT_PATH_LENGTH = 4096

#: 绝不允许出现在登记值里的字符。
#:
#: 两类：
#:
#: 1. shell 元字符（``$ ` ; | & < > * ? ! \ ' " ( ) { } [ ] ~ #``）与空白 ——
#:    Registry **从不**把权重路径交给 shell，登记一个含这些字符的值说明调用方
#:    预期它会被 shell 解释，那是另一种接口，不是这个；
#: 2. 控制字符（含 ``\x00``）—— NUL 是截断类漏洞的经典载体。
_FORBIDDEN_CHARS = re.compile(r"""[\x00-\x1f\x7f\s`$;|&<>*?!\\'"(){}\[\]~#]""")


def validate_weight_path_reference(raw: object) -> str:
    """校验并归一化权重路径**引用**，返回规范字符串。

    绝不访问文件系统。失败抛 :class:`InvalidRequest`。
    """
    if not isinstance(raw, str):
        raise InvalidRequest("weight path must be a string")

    candidate = raw.strip()
    if not candidate:
        raise InvalidRequest("weight path must not be empty")
    if len(candidate) > MAX_WEIGHT_PATH_LENGTH:
        raise InvalidRequest(f"weight path exceeds {MAX_WEIGHT_PATH_LENGTH} characters")
    if _FORBIDDEN_CHARS.search(candidate):
        raise InvalidRequest(
            "weight path contains shell metacharacters, whitespace or control characters"
        )
    if not candidate.startswith("/"):
        # 相对路径的含义依赖进程 CWD —— 一个登记时无法确定的东西。
        raise InvalidRequest("weight path must be absolute")

    segments = candidate.split("/")
    if any(seg == ".." for seg in segments):
        raise InvalidRequest("weight path must not contain '..' segments")

    normalized_segments = [seg for seg in segments if seg not in ("", ".")]
    if not normalized_segments:
        raise InvalidRequest("weight path must name a file")
    return "/" + "/".join(normalized_segments)


def weight_path_fingerprint(path: str) -> str:
    """路径引用的稳定指纹（sha256 前 16 位十六进制）。

    用途是**核对**而不是**隐藏**：owner 可以用它确认两份登记指向同一个文件，
    而下游（路由配置、审计、非 owner 的响应）只看到指纹。
    """
    if not isinstance(path, str):
        raise InvalidRequest("weight path must be a string")
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def redact_weight_path(path: str | None) -> dict:
    """把权重路径降级成「非 owner 可见」的形态。

    非 owner 得到的是 ``{"weight_path": None, "redacted": True, ...}``：
    服务被共享了，owner 的家目录结构没有。
    """
    if path is None:
        return {
            "weight_path": None,
            "weight_path_sha256": None,
            "weight_path_redacted": False,
            "weight_path_present": False,
        }
    return {
        "weight_path": None,
        "weight_path_sha256": weight_path_fingerprint(path),
        "weight_path_redacted": True,
        "weight_path_present": True,
    }


def reveal_weight_path(path: str | None) -> dict:
    """owner（或全局只读管理员）可见的形态：原样给出登记值 + 指纹。"""
    if path is None:
        return {
            "weight_path": None,
            "weight_path_sha256": None,
            "weight_path_redacted": False,
            "weight_path_present": False,
        }
    return {
        "weight_path": path,
        "weight_path_sha256": weight_path_fingerprint(path),
        "weight_path_redacted": False,
        "weight_path_present": True,
    }
