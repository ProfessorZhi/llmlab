"""Atlas 数据契约的离线回归套件。

本包只测**纯函数**：不监听端口、不连生产 Gateway、不读真实 ``usage.jsonl``。
输入全部是 ``fixtures/`` 下团队自写的合成样本，或测试里现构造的字符串。

与 ``tests/auth``、``tests/integration_auth`` 一样是常规包（本仓库的
``tests/__init__.py`` 让 ``tests`` 成为常规包，因此 ``tests/backend`` 也必须
自带一个），这样 ``python -m unittest discover -s tests/backend -t .`` 能直接跑。
"""
