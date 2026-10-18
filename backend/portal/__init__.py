"""Portal 模块契约：在已认证会话之上，回答「这个模块现在能对**你**说什么」。

与 :mod:`backend.atlas` 的关系：Atlas 是**数据面**（调用元数据的归属与聚合），
本包是**控制面**（模块可用性、只读入口、可见性）。两者共用同一批信任原语 ——
``RequesterContext``、``KeyDirectory``、``AccessDenied`` —— 而不共用产出。

**本包的核心立场是 fail-closed：**

* 一个模块只有在**它的前置条件全部成立**时才可能是 ``available``；缺任何一项就是
  ``unavailable`` / ``disabled`` / ``unknown``，并且必须说明原因；
* 一个**链接**只有在它自己的前置条件也成立时才出现。没有「先渲染再禁用」的链接 ——
  出现在页面上的链接就是服务端认为你现在可以点的；
* 一个**数字**只有在它有**已声明的可信来源**时才出现。没有来源就是 ``null`` +
  ``unknown``。GPU 显存尤其如此：本构建没有 NVML 观测源，所以它**永远是** unknown，
  不存在任何一条能把一个看起来正常的显存数字送上页面的代码路径。
"""

from .modules import (
    MODULES_SCHEMA_VERSION,
    DeploymentFacts,
    EndpointRecord,
    ModuleRequest,
    build_modules_dto,
    validate_modules_dto,
)

__all__ = [
    "MODULES_SCHEMA_VERSION",
    "DeploymentFacts",
    "EndpointRecord",
    "ModuleRequest",
    "build_modules_dto",
    "validate_modules_dto",
]
