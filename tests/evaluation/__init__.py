"""评测中心契约的回归测试。

范围：``backend/evaluation/**`` 与 ``dev/evaluation_preview.py`` 的平台层行为。
**不**测试 EvalScope 本体——本仓库里不存在它，测试也不会假装存在。

这组测试的三条自我约束
----------------------

**不碰 GPU、不联网、不起模型。** 所有测试进程按约定以 ``CUDA_VISIBLE_DEVICES=''``
运行；测试里没有任何 ``socket.connect`` 到外部主机、没有 ``nvidia-smi``、没有权重加载。
唯一的真实 socket 是本机环回上的一次 ``wsgiref`` 回环（见 ``test_http_integration``），
它验证的是 HTTP 契约本身。

**不读真实数据。** 没有 ``usage.jsonl``、没有提示词、没有图像、没有真实报告文件。
报告路径的测试用的是 ``tempfile`` 里自己造的合成文件。

**身份是显式声明的合成事实，不查询宿主 NSS。** :data:`HOST_USERS` 是 UID 的
**唯一**声明处，``backend/evaluation`` 只接受显式 UID、从不调用 ``pwd``，因此
"谁是谁"由声明决定，而不是由运行测试的那台机器的 ``/etc/passwd`` 决定。``dana``
不在声明里，所以它也不在任何一张表里——这一点本身就是一条断言（见
``test_identity_and_rbac``）。

运行方式（在仓库根目录）::

    CUDA_VISIBLE_DEVICES='' python -m unittest discover -s tests/evaluation -t . -v
"""

from __future__ import annotations

#: 合成主体 -> UID 的**声明表**。这是 UID 的唯一来源：产品侧只消费显式值，
#: 不对宿主 ``/etc/passwd`` 做任何查询，所以断言的是声明与实际消费的一致性，
#: 而不是某一台机器的账户布局。``dana`` 刻意缺席（``bob`` 才是那个人）。
#: 与 ``tests/registry/__init__.py`` 记录的同一批事实保持一致。
HOST_USERS: tuple[tuple[str, int], ...] = (
    ("carol", 4000),
    ("alice", 4001),
    ("dave", 4002),
    ("bob", 4003),
)

#: 三个模型用户（carol 是 infra admin，单独列出）。
MODEL_USERS: tuple[str, ...] = ("alice", "dave", "bob")

#: infra admin。
ADMIN_USER = "carol"
