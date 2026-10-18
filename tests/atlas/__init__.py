"""Calls / Atlas Lane #77 的独立测试包（``backend/atlas/**` 的接入 seam）。

与 ``tests/backend/`` 下既有的 Atlas 契约测试分开：那里的用例锁定的是
**已接线模块的判定**（授权、聚合、隐私边界），这里的用例锁定的是本 Lane 新交付的
**接线 seam 本身** —— 受信 Key Directory 的加载与 Gateway 语料的有界读取。

分开的直接理由是所有权：``backend/atlas/**`` 与 ``backend/observability/gateway.py``
由本 Lane 排他拥有，而 ``tests/backend/`` 是多个 Lane 共用的目录。
"""
