# 公开仓树安全门禁

`llmlab` 是公开仓库，内容来自已归档的私有来源。迁移审查已经确认：**私有日志、真实
prompt/response、主机身份材料、凭据、原始 benchmark 样本都不进入这棵树**。本文件
记录这条边界如何被自动化守住。

## 规则

`tools/security/public_tree_scan.py` 对 `git ls-files` 列出的文本文件执行七类内容检查，
并对每个被选路径先做 fail-closed 的路径安全校验。命中只报告规则、路径和行号，不把
命中的文本复制进输出。

## 扫描范围：当前 tracked worktree，不是 Git history

本工具只读**当前工作树中 `git ls-files` 列出的文件**。它**不遍历 Git history**，也
不是 gitleaks / trufflehog 的替代品：`clean` 只说明当前 checkout 的 tracked tree 没有
命中，**不证明**历史 commit、已删除 blob、旧分支或远端对象是干净的。

历史 secret scan 是独立的控制面，**本轮没有执行**，也不在本 PR 的交付范围内。任何
“已完成历史扫描”的表述都不成立。

## 默认选择：只读 `git ls-files`；失败即 exit 2

默认 `scan_root(root)` 只运行 `git ls-files -z` 来选择文件。以下任一情况都抛
`ScanReadError`，CLI 以退出码 `2` 失败：

- `git` 不可用；
- root 不是 Git 仓库；
- `git ls-files` 非零退出；
- 命令超时。

**绝不 fallback 到 `os.walk`/`rglob`**：失败时不会遍历目录，因此 ignored、untracked
文件和 `.git` 都不会被读取。测试 fixture 必须显式传 `paths=`，或在临时目录初始化 Git
repo 并 `git add` 被测文件；生产/默认模式不应自行合成文件列表。

## 路径安全：unsafe 即失败，不静默跳过

`scan_root` 对每个选中路径先验证，再读取：

- 拒绝绝对路径、Windows drive 前缀、`..`、`.`、空组件、反斜杠与 NUL；
- 从仓库根开始逐段 `lstat`，拒绝任何 symlink 或 Windows reparse point 组件；
- `resolve(strict=True)` 后必须仍位于仓库根内；
- 最终必须是 regular file；目录、FIFO、设备等一律拒绝；
- 读取使用 `O_NOFOLLOW`（平台支持时）；`stat/open/read/resolve` 任何失败都抛
  `ScanReadError` / `UnsafePathError`，CLI 以退出码 `2` 失败。

因此，受追踪的 symlink 不可能把扫描带到仓库外；无法安全读取的文件也不会被当成
“没有命中”而静默放行。测试用仓库外的合成文件证明：链接被拒绝，目标内容没有被读取。

| 规则 | 拒绝的内容 |
|---|---|
| `private-key` | 私钥头（RSA / OpenSSH / EC / DSA / PGP） |
| `credential` | GitHub / Slack / AWS / `sk-` 形态的 token，以及带数字的 Bearer token |
| `private-source-identifier` | 迁移审查中确认的旧私有来源标识符 |
| `device-reference` | 匹配到的 `device:` 值本身不是精确占位符（`synthetic-*` / `example-*` / `placeholder-*` / `{{...}}`） |
| `absolute-home-path` | 匹配到的家目录名不是文档示例身份（`alice` / `bob` / `carol` 等）；同一行别处的 `{{...}}` 不会豁免 |
| `content-capture` | `contentlog.py`，或代码中新增 `full-body capture` / `request-body capture` / 持久化 prompt/response 的默认路径（旧私有 PR #17 已明确拒绝，不得以任何默认行为重新引入） |
| `raw-artifact` | 不在 `tests/**/fixtures/`、`dev/fixtures/` 下的 `*.jsonl`、`*.sqlite*`、`*.db`、`*.log` |

合成样本的允许方式只有两种：放在仓库已经记录的 fixture 目录，或让**匹配到的值本身**
是精确占位符（`{{...}}`、`synthetic-*`、`example-*`、`placeholder-*`，或测试替身
`not-in-the-directory`）。**整行**出现 `{{...}}` / `synthetic` / `example` /
`placeholder` **不会**豁免同一行里的真实 token、device 或家目录；混合行有独立负测。
**没有行内忽略注释**：新增例外必须改扫描器的显式允许列表并同时补测试，这样例外本身
可被审查。

## 运行

```bash
python3 tools/security/public_tree_scan.py --root .
python3 tools/security/public_tree_scan.py --root . --json
python3 -m unittest tests.security.test_public_tree_safety -v
```

退出码：`0` 干净，`1` 有内容命中，`2` Git 选择失败、路径/读取失败或参数错误。任何
fail-closed 扫描失败都不是“跳过该文件后 clean”。测试套件会随全量 Python 测试一起运行。

## 边界

这是**回归门禁**，不是安全边界。文本扫描无法证明一个刻意规避的作者没有写入秘密；
它保证的是：迁移审查已经点名的标识符、凭据形态、内容捕获模块和原始数据路径，不会
因为一次复制粘贴重新出现在公开 diff 里。真正的凭据边界仍由部署与权限系统承担。

## 已知清理

- `docs/integrations/REGISTRY_SEAM_HANDOVER.md` 与 `docs/v1/V1_68_OFFLINE_GATE.md`
  不再点名开发主机或其 `device:` 标识；复现命令改为与主机无关的描述。
- 测试中的合成路径改用文档示例身份，避免扫描器把占位路径误判为真实家目录。
