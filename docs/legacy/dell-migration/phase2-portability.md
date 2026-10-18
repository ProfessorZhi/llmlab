# 第二阶段 · Windows → Linux 可移植性审计

**结论先行：这不是"能不能跑"的问题，是"照抄会坏在哪"的问题。**
审计覆盖 `portal/`、`integrations/`、`scripts/`、`config/`、`data/`、`reports/` 全部 53 个在册文件。

代码本身对 Linux 的适配度**比预期高**：没有 `shell=True`，没有 `os.startfile`，
进程/端口/路径的安全判定都写成了可移植形式，`_pid_alive()` 与 `subprocess` 调用
**已经带 Linux 分支**。真正会坏的是 **7 处硬编码** 和 **1 个架构性多余**。

---

## 零、最重要的两条结论

### 0.1 本 Portal 的 SSH 探针与隧道，在 Dell 上整段变成多余

现在这套设计的根因是"Portal 在 Windows，节点在 Dell"，所以需要：

| 组件 | 存在理由 | 迁到 Dell 后 |
|---|---|---|
| `probe_ssh()` + `REMOTE_PROBE`（`server.py:498-560`）| 隔着网络读节点指标 | **节点就是本机** ⇒ 直接读 `/proc`、`getloadavg()`、`shutil.disk_usage()` |
| `[tunnels.strata-native]` 18081 转发（`server.py:900-987`）| 浏览器够不到 Dell 的 8080 | **Strata 就在同一台机器上**，且已监听 `{{LAB_BIND_ADDR}}:8080` ⇒ 直连 `localhost:8080` |
| `{{LAB_SSH_KEY_PATH}}` 私钥 | 探针要认证 | **服务器上不该有这把私钥** |

这不是"顺手删掉"，而是**安全收益**：把私钥从服务器上移除，把一条
"自己 SSH 到自己"的回路换成一次本地读。`REMOTE_PROBE` 里 12 条命令
除 `nvidia-smi` 与 `systemctl is-active strata` 外，全部可由 `Path` /
`os` / `shutil` 直接回答——**不必再经 shell**。

> `systemctl is-active strata strata-eval` 在这台机器上**恒为 inactive**
> （第一阶段已实测：Strata 活在 `session-241.scope`，不是 systemd 单元）。
> 迁过去后这条探针应当换成"进程是否存活 + 8080 是否在听"，否则
> 监控页会一直报一个不存在的故障。

### 0.2 `main()` 会**拒绝在局域网上启动**——这正是需求 (5) 的前置项

```python
# portal/server.py:2151-2154
if host not in ("localhost", "localhost", "::1"):
    print("[portal] REFUSING to bind %s: this console has no authentication and "
          "the brief requires localhost." % host, file=sys.stderr)
    return 2
```

`scripts/start.ps1:216-217` 有一份**独立复制的**同款拒绝。

⇒ 统一入口 `http://{{LAB_HOST}}:8765` 在**当前代码下不可能启动**。
这条守门不是障碍，是**证词**：它写明了"这个控制台没有鉴权"。
需求 (5) 要求"先做鉴权再暴露"，所以正确的顺序是
**先加鉴权 → 再把守门从"只许 loopback"改成"许 loopback 或已鉴权"**，
而不是先删守门。这一点必须写进实施计划，不能顺手绕过。

---

## 一、文件路径

| # | 位置 | 现状 | Linux 处置 |
|---|---|---|---|
| P1 | `portal/server.py:808-809` | `ROOT/"evaluation"/".venv"/"Scripts"/"python.exe"`、`.../"evalscope.exe"` | `bin/python`、`bin/evalscope`，**无 `.exe`**。建议改成"探测两个候选"，而不是二选一硬编码 |
| P2 | `config/llm-lab.example.toml:85` | `launch = ["evaluation/.venv/Scripts/evalscope.exe", "service", ...]` | 改成绝对路径 `{{LAB_ROOT}}/services/evalscope/.venv/bin/evalscope`，或保留相对形式但指向 Linux 布局 |
| P3 | `config/llm-lab.example.toml` | `[lab] role = "windows-workstation"` | 语义已过时，改 `dell-node` |
| P4 | `portal/server.py:246` `BENCH_ROOT = ROOT/"data"/"benchmarks"`；`:1239` `logs = ROOT/"logs"`；`:1261-1262` `run/*.pid` | 状态目录钉死在源码树里 | **代码树与状态树必须分离**：代码只读检出，`data/`、`logs/`、`run/`、`data/cache`、`data/metrics` 落到 `{{LAB_ROOT}}/services/run/` 等可写目录。否则 `{{LAB_ADMIN_USER}}` 更新代码时会踩到 `{{LAB_USER}}` 正在写的 pid 文件 |
| P5 | `portal/server.py:303,405,825,842,862,2036` | `.replace("\\","/")` 归一化 | **保留**。在 Linux 上是 no-op，删掉反而丢可移植性 |
| P6 | `portal/server.py:294` | `os.sep in run or "/" in run or "\\" in run` | **正确**，Linux 下 `os.sep == "/"`，两个分隔符仍都被拒 |
| P7 | `portal/server.py:855` | `os.path.commonpath([real, root]) != root` | 按路径分量比较，`/srv/llm-lab-evil` 不会误判为 `/srv/llm-lab` 的子路径。**正确** |
| P8 | `portal/server.py:69,848` | `DENY_NAMES = {"secrets.env", "local.toml"}`，`parts[-1] in DENY_NAMES` | **Windows 不区分大小写、Linux 区分**。`Secrets.env` 在 Linux 上**不命中**这条名单。目前仍会被前缀白名单挡下（fails closed），但这是**靠第二道闸兜底**，应把 `DENY_NAMES` 比较改成 `casefold()`。`DENY_RE`（`:70`）已带 `re.I`，不受影响 |
| P9 | `PUBLIC_FILES`/`PUBLIC_PREFIXES`（`:71-72`） | 字面量前缀匹配 | Linux 下 `Reports/x.md` 会 404（Windows 下会命中）。**fails closed，可接受**，但要在文档里写清"路径大小写敏感" |

---

## 二、PowerShell 依赖

| # | 位置 | 现状 | Linux 处置 |
|---|---|---|---|
| S1 | `portal/server.py:821-822` | `env_report()["tools"]["powershell"]` 跑 `powershell -NoProfile -Command $PSVersionTable...` | Linux 下返回 `unavailable (FileNotFoundError)`，**会在环境自检页显示一条假故障**。改为报告 `bash --version`，或整项删除 |
| S2 | `scripts/start.ps1`（542 行）| 端口预检 `Get-NetTCPConnection`、配置解析、Python 发现（`.venv\Scripts\python.exe` / `py` 启动器）、隐藏启动 portal、开浏览器、起 SSH 隧道、POST `/api/service/start` | **整套需要 Linux 版**：端口→`ss -ltn`；配置解析→直接用 `tomllib`（比正则更安全）；Python 发现→`uv venv` 路径；开浏览器→无桌面会话，**只打印 URL**；隧道→见 §0.1，**取消** |
| S3 | `scripts/stop.ps1`（150+ 行）| `Get-CimInstance Win32_Process` 读命令行做**归属校验**；`taskkill /T /F` 杀进程树 | **这套归属校验是精华，必须原样保留语义**：Linux 用 `/proc/<pid>/cmdline` 读命令行、比对 `Owned` 表里的特征串；`taskkill /T` → `kill -TERM -- -<PGID>`（需 §三 的 `start_new_session`） |
| S4 | `scripts/check-env.ps1:39-40,143` | 探测 `.venv\Scripts\python.exe`、`Scripts\evalscope.exe` | 与 P1 同源，随 P1 一起改 |
| S5 | `deploy/evalscope/install-evalscope.ps1` | Windows 装 EvalScope | **已被 `deploy/templates/node/22-evalscope.sh` 取代**（uv + 3.12 + tuna 索引），无需移植 |
| S6 | `README.md:22-34,113,120-122`；`scripts/{start,stop,check-env,install-evalscope}.ps1:11-23` | 全套安装/启停说明用 PowerShell；并解释"PowerShell 5.1 把无 BOM 的 `.ps1` 按 GBK 读" | 用户已定调：`.ps1` **降级为可选迁移辅助工具**。README 需要并列出 Linux 正式路径 |
| S7 | `config/llm-lab.example.toml:74,83` | 注释指向 `scripts/start.ps1` | 注释级，随 S2 更新 |

**这 4 个 `.ps1` 不是"留着就行"**：它们是当前**唯一**的服务生命周期实现。
在 Linux 正式方案里若没有等价物，"EvalScope 独立启停按钮"（需求 7）
和"服务端管理生命周期、不依赖 Windows 在线"（需求 7）就落不了地。

---

## 三、进程管理 —— 本轮唯一的**功能性**缺陷

| # | 位置 | 现状 | Linux 处置 |
|---|---|---|---|
| J1 | `portal/server.py:1246-1257` | `flags = 0x8｜0x200`（`DETACHED_PROCESS｜NEW_PROCESS_GROUP`）**只在 `os.name=="nt"` 时生效**；其余情况 `flags = 0` | **这是照抄必坏的一处。** Linux 上 `flags=0` ⇒ EvalScope 与 Portal **同进程组**：按组杀 Portal（S3 的 `kill -- -PGID`）会连 EvalScope 一起带走，Ctrl-C 亦然。等价物是 `start_new_session=True`（`setsid`），使 `pid == pgid == sid`，同时也让 S3 的组杀**精确** |
| J2 | `portal/server.py:925-957` `_pid_alive()` | `os.name != "nt"` → `os.kill(pid, 0)`；Windows → `OpenProcess`/`GetExitCodeProcess` | **已可移植**。注意 `os.kill(pid,0)` 对**僵尸**返回成功、对**他人进程**抛 `PermissionError`（`OSError` 子类 → 判死）。同用户场景下无碍，作为已知保真度缺口记录 |
| J3 | `portal/server.py:940-945` | `ctypes.WinDLL` 在 `os.name=="nt"` 分支内 | **已正确隔离**，Linux 上不会被导入 |
| J4 | `portal/server.py:1229-1265` `start_service()` | 只执行**配置里已逐字存在**的 argv，无 shell、无参数插值 | **这是 §六 安全要求的实现，必须原样保留**。迁到 Linux 后仍只许执行配置里的 argv |
| J5 | `portal/server.py:1262` | `run/<key>.pid` 写 `proc.pid` | 迁 Linux 后建议**同时记 pgid**（`start_new_session` 下与 pid 相同），并在 pid 文件里显式写出，避免 S3 依赖"pid==pgid"这个隐含前提 |
| J6 | 持久性 | 无 | 第一阶段已定：`KillUserProcesses=false` ⇒ 分离进程能活过登出；`Linger=no` ⇒ `systemctl --user` **不可用**。采用**分离进程 + `@reboot` cron**。`start_new_session=True` 是这条方案成立的前提（J1） |

---

## 四、配置

| # | 位置 | 现状 | Linux 处置 |
|---|---|---|---|
| C1 | `portal/server.py:53` `CONFIG_PATH = ROOT/"config"/"llm-lab.toml"` | 配置钉死在源码树 | 部署时应支持 `--config` / 环境变量覆盖，让"只读代码树 + 站点配置"分离 |
| C2 | `config/llm-lab.example.toml [node]` `host/ssh_user/ssh_key/ssh_known_hosts/ssh_timeout_s/ssh_probe_timeout_s` | SSH 探针参数 | 随 §0.1 **整段退役**。私钥不应落在服务器上 |
| C3 | `config/llm-lab.example.toml [tunnels.strata-native]` | 18081 本地转发 | 随 §0.1 **退役**；`_nav()`（`:1977-1990`）里"隧道 URL 优先"的分支简化为直连 `strata_endpoint()["base_url"]` |
| C4 | `config/llm-lab.example.toml [endpoints.strata-local] base_url = "http://{{LAB_HOST}}:8080"` | 走节点 IP | 本机应改 `http://localhost:8080`；局域网用户若要开 Strata 原生界面，走**已存在的 `{{LAB_BIND_ADDR}}:8080`**，不经 Portal |
| C5 | `config/llm-lab.example.toml [services.evalscope] cwd = "evaluation"` | 相对代码树 | 改指节点侧 EvalScope 目录；`EvalScope` 的 `outputs/` 扫描根随之落到 `/srv/...` |
| C6 | `[portal] host = "localhost"` | 见 §0.2 | **先加鉴权，再放宽**。`main()` 与 `start.ps1` **两处**守门都要改，别只改一处 |
| C7 | `read_secret(var, "config/secrets.env")`（`server.py:101`、`487-488`、`1329-1332`）| 相对 `ROOT` 解析 | 契约已定：`{{LAB_ROOT}}/services/secrets/strata-key.env`，`{{LAB_ADMIN_USER}}:{{LAB_GROUP}}`，`640`。注意第一阶段实测：现有 `configs/api-keys.env` 是 `-rw-------+1 {{LAB_ADMIN_USER}} {{LAB_GROUP}}`，**`{{LAB_USER}}` 读不到**——新文件必须靠 `{{LAB_GROUP}}` 组授权，不能靠 uid |
| C8 | `DENY_NAMES` 含 `secrets.env`、`local.toml` | 与 `.gitignore` 对齐 | `config/local.toml` 是 Windows 本机覆盖机制的痕迹，Linux 上可用同一套，但要确认新路径也在 `DENY_NAMES` 内 |

---

## 五、编码

| # | 位置 | 现状 | Linux 处置 |
|---|---|---|---|
| E1 | `portal/server.py:256-266` `_read_text()` | 依次试 `utf-8 → gb18030 → latin-1` | **必须保留**。历史报告/benchmark 是在 Windows 上产出的，字节可能是 GBK；迁到 Linux 只是搬家，**字节不会自己转码**。这个回退链是迁移安全网 |
| E2 | `portal/server.py:1244-1245` | 为子进程设 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`（注释：GBK 控制台会让带 emoji 的 banner 抛 `UnicodeEncodeError`）| **保留**。Linux 下通常已是 UTF-8，但这两行仍是正确且廉价的保险 |
| E3 | `portal/server.py:916` 读 / `:1262` 写 pid 文件 | `encoding="ascii"` | 两边都正确 |
| E4 | `portal/server.py:802-803` | `subprocess.run(..., encoding="utf-8", errors="replace")` | 正确 |
| E5 | `README.md:113,120-122`；`scripts/*.ps1:11-23` | GBK/ANSI `.ps1` 解码陷阱 | **Windows 专属**，不要误读成 Linux 要求。用户已把 `.ps1` 降级为可选工具 |
| E6 | Portal 自身 stdout | 打印 `[portal] ... listening on ...` | 目前纯 ASCII。启动器应显式导出 `PYTHONIOENCODING=utf-8`，避免日后文案加中文时报错 |

---

## 六、Shell 调用

| # | 位置 | 现状 | 处置 |
|---|---|---|---|
| H1 | 全树 | **没有 `shell=True`**（已逐文件确认） | 迁 Linux 后**继续保持** |
| H2 | `portal/server.py:800-806,1229,1254-1257` | 一律 argv 列表 | 可移植，保持 |
| H3 | `portal/server.py:511-525` `REMOTE_PROBE` | 一整串 shell 交给远端 ssh 执行 | **若改为本地探针，最安全的做法是不再拼 shell**，而是拆成若干 `subprocess.run([...])` / 直接读 `/proc`、`getloadavg()`、`shutil.disk_usage()`，彻底去掉"字符串里的管道和 `grep`" |
| H4 | `portal/server.py:1915-1916` | POST 需 `X-Requested-With: llm-lab-portal`，否则 403 | **同源 CSRF 门，可移植且必须保留**。注意：这是"拒绝跨源"，**不是鉴权**——它挡不住一个直接 `curl` 的局域网用户，所以不能满足需求 (5) |

---

## 七、迁到 Dell 时**不能改坏**的不变量

1. `start_service()` 只执行配置里已存在的 argv，**前端无法传入任何命令**（J4 / H2）。
2. 静态资源两道闸：`DENY_NAMES`/`DENY_RE` + 前缀白名单（P8）。
3. `_resolve_public()` 的 `commonpath` 越界判定（P7）。
4. 前端**永远拿不到** API Key / SSH Key：`key_present` + `key_sha256_12` 指纹（`server.py:1327-1335`）是唯一出口。
5. `_read_text()` 的 `gb18030` 回退链（E1）——历史数据不会自动转码。
6. `PYTHONUTF8/PYTHONIOENCODING`（E2）。
7. v2 信封的 `ok=false` 仍回 HTTP 200；**不许**用状态码替代 `ok`（`server.py:1282-1297`）。

---

## 八、结论

- **可以直接搬**：`portal/web/**`（纯 ES module + CSS，零路径耦合）、`integrations/**`（HTTP 适配器，无 OS 依赖）、`data/benchmarks/**` 与 `reports/**`（纯数据）。
- **必须改**：`portal/server.py` 的 **7 处**（P1、S1、J1、J4 的 pid/pgid、C1、C6、§0.1 的探针与隧道退役）。
- **必须重写**：`scripts/*.ps1` 的 4 个生命周期功能 → Linux 版（S2、S3）。
- **架构性简化**：SSH 探针 + 18081 隧道**整段消失**，同时**把私钥从服务器上移除**。
- **唯一的功能性 bug**：`J1`（`creationflags` 无 Linux 等价物）——不做，EvalScope 会随 Portal 一起死。
- **需求 (5) 的前置**：`C6`（先鉴权、再放宽 loopback 守门，且 `main()` 与 `start.ps1` 两处都要改）。

---

*本审计为只读。执行过的写操作仅限 `{{LAB_WORKTREE}}` 本机仓库与 Dell 上的
`{{LAB_ROOT}}/services/` 空目录树（第一阶段已披露）。未触碰 Strata、
NVIDIA 驱动、系统包或任何 systemd 单元。*
