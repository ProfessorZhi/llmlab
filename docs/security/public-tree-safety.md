# 公开仓树安全门禁

`llmlab` 是公开仓库，内容来自已归档的私有来源。迁移审查已经确认：**私有日志、真实
prompt/response、主机身份材料、凭据、原始 benchmark 样本都不进入这棵树**。本文件
记录这条边界如何被自动化守住。

## 规则

`tools/security/public_tree_scan.py` 对 `git ls-files` 列出的文本文件执行七类检查。
命中只报告规则、路径和行号，不把命中的文本复制进输出。

| 规则 | 拒绝的内容 |
|---|---|
| `private-key` | 私钥头（RSA / OpenSSH / EC / DSA / PGP） |
| `credential` | GitHub / Slack / AWS / `sk-` 形态的 token，以及带数字的 Bearer token |
| `private-source-identifier` | 迁移审查中确认的旧私有来源标识符 |
| `device-reference` | 非 `example` / `placeholder` / `synthetic` 前缀的 `device:` 引用 |
| `absolute-home-path` | 非文档示例身份（`alice` / `bob` / `carol` 等）的 `/home/<user>`、`/Users/<user>`、`/data/home/<user>` |
| `content-capture` | `contentlog.py`，或代码中新增 `full-body capture` / `request-body capture` / 持久化 prompt/response 的默认路径（旧私有 PR #17 已明确拒绝，不得以任何默认行为重新引入） |
| `raw-artifact` | 不在 `tests/**/fixtures/`、`dev/fixtures/` 下的 `*.jsonl`、`*.sqlite*`、`*.db`、`*.log` |

合成样本的允许方式只有两种：放在仓库已经记录的 fixture 目录，或使用可见的
`{{PLACEHOLDER}}` / `example` / `synthetic` 形式。**没有行内忽略注释**：新增例外必须
改扫描器的显式允许列表并同时补测试，这样例外本身可被审查。

## 运行

```bash
python3 tools/security/public_tree_scan.py --root .
python3 tools/security/public_tree_scan.py --root . --json
python3 -m unittest tests.security.test_public_tree_safety -v
```

退出码：`0` 干净，`1` 有命中，`2` 参数错误。测试套件会随全量 Python 测试一起运行。

## 边界

这是**回归门禁**，不是安全边界。文本扫描无法证明一个刻意规避的作者没有写入秘密；
它保证的是：迁移审查已经点名的标识符、凭据形态、内容捕获模块和原始数据路径，不会
因为一次复制粘贴重新出现在公开 diff 里。真正的凭据边界仍由部署与权限系统承担。

## 已知清理

- `docs/integrations/REGISTRY_SEAM_HANDOVER.md` 与 `docs/v1/V1_68_OFFLINE_GATE.md`
  不再点名开发主机或其 `device:` 标识；复现命令改为与主机无关的描述。
- 测试中的合成路径改用文档示例身份，避免扫描器把占位路径误判为真实家目录。
