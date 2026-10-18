# 安全模型

## 五条规则

### 1. 全部绑定 `localhost`

`start.ps1` 解析配置后**拒绝**非 loopback 的 `[portal].host`：

```
REFUSING: config binds '<host>'. This console has no authentication;
the brief requires localhost.
```

理由：控制台没有鉴权。把它绑到网络可达的地址，等于把节点的管理面交出去。

### 2. 密钥从文件读，绝不作为参数传递

* `config/secrets.env` 只由**服务端**读取；
* 密钥值**不落日志、不进异常消息、不进响应体、不进报告、不进截图**；
* 对外只暴露**指纹** `sha256[:12]`——足以回答「加载的是哪把钥匙」，但答不出钥匙本身。

`integrations/` 的适配器契约里，凭据永远是一个**路径参数**，不是值参数。

### 3. 全项目只有一条远程命令

`portal/server.py` 的 `REMOTE_PROBE` 是一个硬编码常量，只读 `/proc` / `df` / `ss` /
`pgrep` / `systemctl is-active` / `nvidia-smi`。

**不提供任意 shell 执行能力。** 控制台没有「执行命令」的接口，也没有任何路由会把
请求内容拼进远程命令。`docs/architecture.md` 说明了为什么需要这条探针（systemd 单元名
不是事实来源，端口监听才是）。

### 4. SSH 主机密钥固定（pinned）

* 项目内 `{{LAB_KNOWN_HOSTS_PATH}}` + `StrictHostKeyChecking=yes`；
* `IdentitiesOnly=yes` —— 只用本项目这把钥匙，不用 agent 里的其他钥匙；
* `BatchMode=yes` —— 绝不弹交互提示；
* **自动化脚本不接受未经验证的主机密钥**，不写入 {{LAB_ADMIN_USER}} 管理员密码。

#### 一个必须记住的坑：一把主机会提供多种密钥类型

`ssh-keyscan` 对同一台主机**每种密钥类型写一行**（rsa / ecdsa / ed25519）。最初
`verify_pinned_fingerprint()` 取了**第一行**，也就是 `ssh-rsa`，然后拿它去比我们固定的
**ed25519** 指纹 ⇒ 报 `match: false`。

**这看起来和一次真实的中间人攻击一模一样。** 一个假阴性伪装成安全事件，比一个漏报
更浪费时间——它会导致「关掉校验」这种最坏的修复。

现在的实现给该主机的**每一条**记录都算指纹，再问固定值是否**在集合里**，并回显
命中的类型（`matched_key_type: "ssh-ed25519"`）。

> 一般规律：**量具会报假阴性。** 任何「安全闸门」在报告失败之前，都要先自证它有能力
> 报告成功。

### 5. 不做遥测表演

遥测拿不到时，**如实显示故障原因**，不用历史数据冒充实时指标。

本机 NVML 因驱动/库版本不匹配而不可用（NVML library version 595.99），
`nvidia-smi` 的输出被控制台原样呈现：

```
Failed to initialize NVML: Driver/library version mismatch
```

不给它配一个「看起来更专业」的措辞，也不偷偷换成上次读到的数值。

#### 订正（2026-10-10）：NVML 坏了 ≠ GPU 指标不可用

上面这条曾经被写成「GPU 指标不可用」。**实测把两件事分开了**，这是必须留痕的订正：

| 通道 | 实测 |
|---|---|
| `nvidia-smi` 及一切基于 NVML 的工具 | ❌ 仍然失败 |
| **`Strata /metrics` → `hardware`** | ✅ **有完整实时 GPU 数据**（util / 显存 / 温度 / 功耗 / PCIe） |

原因：Strata 自己是持有 CUDA 上下文的进程，它读显卡走 **CUDA 运行时**而不是 NVML。
库版本错配只杀死外部工具，杀不死进程内自省。

所以控制台的 GPU 磁贴显示的是**真实读数**，来源标注为 `Strata /metrics hardware`，
并与 `/v1/status machine.gpu` 做**双源核对**（实测偏差 `0.0%`）。

> 这条订正有实际后果：**任何 `dcgm-exporter` / `nvidia_gpu_exporter` 方案在本机必然失败**，
> 因为它们全部基于 NVML。见 `docs/grafana-proposal.md` §2.1。
>
> 一般规律：**一个工具坏了，不能推出它代表的那类数据都拿不到。** 要先问「还有谁在读它」。

### 6. 节点原生 UI 的凭据落在浏览器里（如实披露）

控制台把 Strata 原生 UI 做成链接打开，**不做 iframe 嵌入**。原因是必须让用户知道：
那个 UI 会把东西写进**该源的 `localStorage`**，而且明文。

反编译其前端（`/web/app.js`）实测，它在 `localStorage` 下用 `strata.` 前缀存四样东西：

| 键 | 内容 |
|---|---|
| **`strata.apikey`** | **API Key 明文**。`headers()` 读它拼 `Authorization: Bearer <key>` |
| `strata.chat` | 聊天记录 |
| `strata.sampling` | 采样参数 |
| `strata.theme` | 主题 |

**关键在「源」而不是「盘」**：`localStorage` 按源隔离，所以

* 经隧道访问 → 键落在 `http://localhost:18081` 这个源下；
* 走直连 → 落在 `http://{{LAB_HOST}}:8080` 这个源下。

两者**互不可见**。用户若在原生 UI 里手输过 Key，它就明文躺在浏览器配置目录里，
直到手动清除该源的数据为止。

**本控制台绝不代替用户注入 Key**——Key 只由服务端读取，前端拿到的是指纹
`sha256[:12]`。原生 UI 里的 Key 是**用户自己敲进去的**，这一点在 UI 上有说明文字。

## brief 明令禁止、本项目也**没有**做的事

| 禁止项 | 本项目的实际做法 |
|---|---|
| 重启服务器 / 重装驱动 | 无任何重启或驱动相关代码 |
| 修改 Strata 配置或运行参数 | 适配器是只读的（契约第 3 条） |
| 修改模型文件 | 不接触模型目录 |
| 大型 Benchmark / 生产 API 压测 | `start.ps1` **不自动启动 Benchmark**；控制台加载页面**绝不调用** `/v1/chat/completions` |
| 飞书写入 | `publish()` 无条件抛异常，见 `docs/feishu-publish-interface.md` |
| 把管理员密码写进脚本 | 未写入任何位置 |
| 未验证的 SSH 主机密钥 | 固定指纹 + `StrictHostKeyChecking=yes` |
| 修改全局 Python | EvalScope 装在 `evaluation/.venv`，全局 Python 未动 |

## 哪些是**可验证**的，而不是声明

`scripts/selftest.py` 用 Python `urllib`（不用 `curl`，见下）打真实的控制台，包含
**13 道安全闸门 + 2 道阳性对照**（当前实测：**50 项检查全通过**）：

| 闸门 | 期望 |
|---|---|
| `/file?p=../../config/secrets.env` | 拒绝 |
| `/download` 绝对路径 | 拒绝 |
| `/file?p=config/secrets.env`（原文） | 拒绝 |
| 同上，URL 编码形式 | 拒绝 |
| SSH 私钥（多种路径写法） | 拒绝 |
| `/file?p=portal/server.py` | 拒绝 |
| `/download?p=portal/server.py` | 拒绝 |
| `/report?p=<源码文件>` | 拒绝 |
| `/api/benchmarks/<../run>` | 拒绝 |
| `/api/export/zip?run=<../run>` | 拒绝 |
| **POST** 无 `X-Requested-With`（CSRF） | 403 |
| **POST** 一个不在白名单里的服务名 | 404 |
| **阳性对照**：一个本来就不存在的正常路径 | 返回 404（证明闸门不是「路由整体坏掉」） |
| **阳性对照**：白名单里的服务名 | 200（证明白名单不是「什么都不放行」） |

### 唯一一个「会做事」的路由及它的闸门

`POST /api/service/start` 是本项目**唯一**能改变本机状态的路由（按需拉起 EvalScope）。
它的门是第一道要求一个**自定义请求头**：

```python
if self.headers.get("X-Requested-With") != "llm-lab-portal":
    return self._err(403, "missing X-Requested-With; refusing cross-origin POST")
```

这不是形式主义。任意网页都能向 `localhost` 发请求，但**跨源 `fetch` 一旦带上自定义头，
浏览器会先发 preflight，而本服务不应答 preflight**——于是跨源调用在真正到达之前就被挡下。
配合 §1 的 loopback 绑定，构成「同机但非同源」与「同网段」两层隔离。

阳性对照不是凑数：没有它，「全部 403」和「服务器根本没起来」在输出上无法区分。

### 为什么量具是 `urllib` 而不是 `curl`

本机上 `curl` 对 `localhost` 上一个**确实可达**的服务返回 `000`，而裸
`socket.create_connection` 和 `urllib` 都成功（HTTP 200）。用 `curl` 做探针会得到
「服务挂了」这种**方向完全相反的结论**。`scripts/selftest.py` 的文档字符串记着这条。

## 本项目对节点**做过**的一处变更（如实披露）

为了让 `{{LAB_USER}}` 的独立密钥可用，在 5820 上为 `{{LAB_USER}}` **新增了一条 `authorized_keys` 记录**
（一次性 sudo 引导完成）。这是：

* **追加式、可逆**的；
* 落在 brief 的禁止清单**之外**（禁止的是驱动 / Strata 配置 / 模型文件 / 后台服务）；
* 是 brief 自己指定的架构（§7 要求先验证 `{{LAB_USER}}` 的独立 SSH Key 是否可用）所需要的。

它**没有**修改任何驱动、Strata 配置、模型文件或后台服务。

## Windows 侧的密钥存放

密钥放在**受保护的本地配置**（`config/ssh/`，已 gitignore）中。若要进一步收紧，可把
私钥移入 Windows 凭据管理器（DPAPI）——**这是后续可选项，当前未做**，因为
`config/ssh/` 的访问控制已由项目目录承担，且移入 DPAPI 会让 `check-env.ps1` 的
信任检查需要额外一层解密步骤。
