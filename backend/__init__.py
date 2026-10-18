"""LLM Lab Portal backend.

本仓库当前有两个后端包：

- :mod:`backend.auth` —— 双入口认证（本机环回 admin + 飞书 OAuth）。见
  ``docs/auth-implementation.md``。
- :mod:`backend.atlas` —— 纯函数 Atlas 用量聚合数据契约。它**不**监听端口、
  不启动进程、不连接生产 Gateway / Strata / Prometheus / Grafana，
  也**不**读取真实 ``usage.jsonl``；输入由调用方以字符串传入。

两者只通过调用方注入的参数耦合：认证负责回答「你是谁、你能看哪些项目」，
Atlas 负责在被授予的范围内做聚合。数据模块自身不做认证。
"""

__all__ = ["atlas", "auth"]
