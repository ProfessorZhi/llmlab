"""Registry 的测试包。

全部测试**离线**运行：不连 GPU、不连生产 Gateway、不连任何真实推理服务、
不读真实 API key 或用户数据。需要「一个活着的 HTTP 上游」时，用本进程内起的
一次性 loopback 假服务，或直接用确定性替身（``StaticProber`` / ``MockGatewayAdapter``）。

身份用的是**合成**的账号与 UID（``carol``=4000 / ``alice``=4001 / ``dave``=4002 /
``bob``=4003）。它们不来自任何一台机器的 ``/etc/passwd``，也不与真实账户对齐；
``dana`` 刻意不在表内，用来证明没有别名折叠。

运行：

    python3 -m unittest discover -s tests/registry -t .
"""

from __future__ import annotations
