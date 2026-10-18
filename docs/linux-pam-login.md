# Linux 用户名 + Linux 密码登录：实现说明与运维手册

本文件说明 `backend/auth_linux/**` 这一层做了什么、怎么配置、怎么安装、怎么验收、怎么回滚，以及**上线前还有哪些事不归本次实现负责**。

范围与所有权：本次改动只落在 `backend/auth_linux/**`、`tests/auth_linux/**` 和本文件。没有改动 `web/**`、`backend/portal/**`、`dev/preview_auth.py`、`backend/auth/**`（V5 认证代码由另一位开发者负责），也没有新增根目录公共文件。除 `tests/__init__.py`（空文件，与既有分支逐字节一致）外，没有在本层之外新增任何文件。

---

## 0. 当前状态（先读这一节）

这一节是**如实记录**，不是「待办」。任何关于本功能「已经能用」的说法都必须先推翻这里的每一条。

| 项 | 状态 |
| --- | --- |
| 真实 PAM 调用 | **未部署**。`pam_backend.py` 中唯一可选的实现是 `UnavailablePamBackend`，它对任何输入都返回 `unavailable`，即**永远拒绝** |
| 认证 broker | **未安装**。没有 `systemd` 单元、没有 socket、没有 `/etc/pam.d/` 条目 |
| 本功能开关 | **默认关闭**（`LLM_LAB_LINUX_AUTH_ENABLED` 不设即为关）。开启前，本层对运行中的 Portal 行为**零影响** |
| 各 Linux 账号是否有可用密码 | **UNKNOWN**。本次实现**没有**、也**不允许**去测试、猜测或验证任何真实账号的口令，因此 `alice` / `bob` / `carol` / `dave` 是否设置了密码、密码是否可用、`pam_unix` 是否接受，全部未知 |
| 测试证据的性质 | 测试全部使用**合成口令**与**合成 PAM 后端**。合成后端返回 `authenticated` **不能**作为「真实密码可用」的证据 |
| 平台角色 | `alice` 的平台管理员角色**仅**来自 root-owned 授权文件，见 §5。当前**尚无**该文件，因此现在即使功能开启，本路径也不产生任何管理员 |

一句话：**这套代码今天不会让任何人登录成功。** 它先把边界、拒绝路径和可审计性做对，真实凭证校验留待 broker 安装后再开。

---

## 1. 为什么需要一个独立 broker

Portal 的 Web 进程以普通账号（`bob`）运行。这带来一个不能靠「多试几次」绕过的现实：

- 普通进程**读不到** `/etc/shadow`；
- Linux PAM 的 `pam_unix` 模块通过辅助程序 `unix_chkpwd` 校验口令，而该辅助程序**只校验调用者自身**的账号。也就是说，让 `bob` 的进程直接调用 PAM，最多只能验证 `bob` 自己，**无法**验证 `alice` 或 `dave`；
- 提高 Web 进程权限（setuid 助手、`subprocess` 调 `su`/`login`、可从任意用户写入的 socket）等于用一个更小的麻烦换来一个更大的麻烦：一旦 Web 应用被拿下，攻击者直接拿到那个助手能做的事。

因此本实现把口令校验放进一个**独立的、受限的、可审计的 broker**：它按 PAM 的需要运行（通常 root），Web 进程只能通过一个 **root 拥有的 Unix domain socket** 找它，且 socket 的调用方由内核提供的 `SO_PEERCRED` 限定。broker 对**白名单内的账号**只做三件事：`pam_authenticate`、`pam_acct_mgmt`、只读 `getpwnam`；返回 UID、登录名和账号是否有效，**不返回**任何口令、哈希或 shadow 记录。它不是一个通用的「以 root 执行」的设施。

**明确不做的事**：不构造 setuid 任意 shell；不通过 `subprocess` 拼接 `su` / `login`；不提高 Web 进程权限；不把 socket 放在任何其他账号可写的目录里。以上每一条都有机械化的源码断言（`tests/auth_linux/test_static_guarantees.py`）。

---

## 2. 组件与文件布局

```
backend/auth_linux/
  errors.py            面向客户端的错误信封（与既有 authservice 同形状）
  reasons.py           内部拒绝原因 → 客户端可见错误的**全函数**映射（防枚举的核心）
  accounts.py          只读 getpwnam / getpwuid；登录名形状校验；UID 交叉核对
  securefile.py        读取 root-owned 策略文件（属主/权限/大小/父目录校验）
  policy.py            白名单、管理员授权、账号策略（root/系统账号/非登录 shell）
  protocol.py          broker 线上协议（分帧、严格 schema、长度上限）
  pam_backend.py       PAM 接缝；默认且唯一可选的实现是「一律拒绝」
  broker_client.py     Web 侧：调用 broker，任何异常都收敛为拒绝
  broker_server.py     特权侧：SO_PEERCRED 校验、策略复检、调用 PAM、返回 UID/名字
  throttle.py          锁定/限速**钩子**（可注入既有 DB 限速器）
  audit.py             结构化审计记录（字段名含凭据语义即构造失败）
  provider.py          AuthProvider 抽象 + Linux/PAM 实现（判定顺序在这一层）
  handoff.py           会话身份交接契约（本层**不**创建会话）
  config.py            环境变量配置，fail-closed
  service.py           组装与对外的 login() 入口（无 HTTP、无 Cookie）

tests/auth_linux/      294 个测试，全部离线
docs/linux-pam-login.md  本文件
```

---

## 3. 安全不变量

以下每条都由测试断言，而不是由流程保证：

1. **无默认放行。** broker 不存在、连不上、超时、回复畸形、回复超长、`SO_PEERCRED` 不可用——全部收敛为 `PAM_UNAVAILABLE`（HTTP 503），而 `PAM_UNAVAILABLE` 从不认证任何人。
2. **机器账号不能登录 Web。** `root` **在任何配置下**都不能登录，哪怕白名单里写了 `root`；UID 低于人类下限（默认 1000）或 shell 为非登录 shell（`nologin` / `false` / 空）的账号，在**接触 PAM 之前**就被拒。
3. **角色从不被推断。** Linux 登录得 `member`。唯一例外是 root-owned 授权文件中 `(host_id, uid, name)` **三者同时匹配**的条目。用户名、请求字段、看起来像管理员的拼写、缺失的文件，都不足以产生 `platform_admin`。
4. **没有别名机制。** `erin` 不是 `dave`。白名单 schema 里**没有**别名字段，出现未知字段整份文件拒绝加载；登录名只按它本身查找。把某人绑到某个真实账号是一次显式的配置动作，不是查找逻辑里的便利功能。
5. **口令只在内存中短暂存在。** 不写入文件、日志、异常、URL、审计记录或命令行。发出的帧在往返结束后被覆写。
6. **空口令在两侧都被拒。** 即使运维把 PAM 栈配成 `nullok`（会把空口令当作正确），Web 侧与 broker 侧都**先于 PAM** 拒绝空口令。
7. **UID 必须双向一致。** broker 的回答只有在「名字→UID」和「UID→名字」都与 Web 侧自己的只读视图一致时才算数。即使 broker（或顶替了 socket 的进程）返回一个格式完全正确、但指向另一个账号的成功回复，也会被拒。
8. **不新增高危会话框架。** 本层不创建会话、不设 Cookie、不签发或校验 CSRF token；它把已验证身份交给既有 authservice，见 §7。

---

## 4. 判定顺序（provider）

顺序本身就是设计，写在 `backend/auth_linux/provider.py` 的模块 docstring 里：

1. **形状**：是不是一个合法的 Linux 登录名？不合法则**不触碰** NSS / PAM / broker。
2. **空口令不是口令**：先拒。
3. **限速**：在昂贵步骤之前计数，且**对不存在的账号同样计数**，这样 `429` 不回答「这个账号存在吗」。
4. **账号是否存在**：只读查找。
5. **该账号是否允许成为 Portal 身份**：策略（`root`、系统账号、白名单）。
6. **此时才问 broker**：唯一一次特权对话。
7. **不相信不自洽的回答**：UID 与名字都必须与本地只读视图一致。

**已知且已记录的残留信号**：第 6 步只对通过策略的账号执行，因此响应耗时**微弱地**反映某个名字是否被策略接纳。之所以可以接受，需要同时成立两点：主机上的 Linux 账号名对任何本地用户本来就可用 `getent passwd` 枚举；白名单里是本平台自己的成员而非机密。若威胁模型变化，这一点必须重新评估。

---

## 5. 策略文件（root-owned）

两个文件**必须由 root 拥有**、不可被 group/other 写入，且**其父目录也必须由 root 拥有且不可被 group/other 写入**（否则 root-owned 文件可被目录属主替换，其余检查形同虚设）。权限不符、文件缺失、超大、内容非法——一律 `ConfigError`，**拒绝启动**，绝不降级为「放行」。

### 5.1 Portal 白名单

```json
{
  "version": 1,
  "allowed_users": ["alice", "bob", "carol", "dave"]
}
```

- 未知字段（例如有人试图加 `"aliases": {"erin": "dave"}`）→ **整份文件拒绝加载**，不会「忽略别名后继续」。
- 空列表 → 拒绝加载（几乎总是半途而废的编辑，而不是刻意的全禁）。
- 名字必须符合 `\A[a-z_][a-z0-9_-]{0,31}\Z`。
- **注意 `\Z` 而非 `$`**：Python 的 `$` 也匹配行尾换行，用 `$` 会让 `"root\n"` 通过校验。

### 5.2 平台管理员授权

```json
{
  "version": 1,
  "host_id": "dell-test-01",
  "platform_admins": [
    {"uid": 1000, "name": "alice"}
  ]
}
```

- `host_id` 必须与本机配置一致：把授权文件复制到第二台主机**不会**带走提权。
- 条目按 `(uid, name)` **同时**匹配。UID 被回收给另一个人、或管理员账号改名，都不再是管理员——这是刻意的失败方向。
- 该文件不存在 = 本路径不产生管理员（**不是**启动失败）。

> **角色模型变更声明（需由 `docs/auth-implementation.md` 的所有者处理）**
> 该文件 §1 明确写着「Linux 用户登录 / Caddy BasicAuth **不能**替代业务管理员」，并规定业务管理员只认本地 `admin` 会话。本实现按本次授权引入了一个**显式的 root-owned 授权文件**作为唯一例外。这是对既有不变量的**有意扩展**，不是一个可以悄悄落地的细节：合入前需要由该文件的所有者更新 §1，写明「Linux 身份可为 `member`；`platform_admin` 仅来自 root-owned 授权文件」。本任务的文件所有权不含该文件，故未改动。

---

## 6. 配置

全部来自进程环境变量，前缀 `LLM_LAB_`。**缺配置一律 fail closed**：不完整或互相矛盾的配置让进程启动失败（`ConfigError`），而不是退化成「看起来能用」。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_LAB_LINUX_AUTH_ENABLED` | 未设 = 关闭 | 必须显式设为 `1`/`true`/`yes`/`on`。**默认关闭是本功能最重要的开关**；拼错（如 `tru`）会报错而不是当作关闭 |
| `LLM_LAB_LINUX_HOST_ID` | 必填 | 本机标识，进入 Principal 键。`\A[a-z0-9][a-z0-9._-]{0,63}\Z` |
| `LLM_LAB_PAM_BROKER_SOCKET` | 必填 | broker 的 Unix socket **绝对路径** |
| `LLM_LAB_LINUX_ALLOWLIST_FILE` | 必填 | §5.1 白名单**绝对路径** |
| `LLM_LAB_LINUX_ADMIN_GRANT_FILE` | 选填 | §5.2 授权文件；不设则无管理员 |
| `LLM_LAB_LINUX_POLICY_OWNER_UID` | `0` | 上述策略文件及其父目录必须具备的属主 UID |
| `LLM_LAB_LINUX_BROKER_TIMEOUT_SECONDS` | `5.0` | 单次 broker 往返上限（0.1–60） |
| `LLM_LAB_LINUX_MIN_UID` | `1000` | 人类账号 UID 下限 |
| `LLM_LAB_LINUX_MAX_FAILURES` | `5` | 窗口内允许的失败次数 |
| `LLM_LAB_LINUX_WINDOW_SECONDS` | `300` | 失败计数窗口 |
| `LLM_LAB_LINUX_LOCKOUT_SECONDS` | `900` | 触发后的锁定时长 |

**限速钩子建议**：`throttle.py` 定义的接口与既有 authservice 的 DB 限速器**签名一致**（`check` / `record_failure` / `record_success`，参数为 `scope, key`），可直接注入，无需适配层。`InMemoryThrottle` 是给「没有数据库」和测试用的**较弱**选项：计数只在一个进程内、重启即清零。生产请注入 DB 版。

---

## 7. 会话交接（复用既有 authservice，不新建会话框架）

本层**不**创建会话。链路是：

```
HTTP 路由（既有 authservice 负责）
   └─► LinuxAuthService.login(login_name, password, client_key)   # 本层
          └─► VerifiedIdentity（login_name, uid, host_id, role, display_name）
   └─► LinuxAuthService.begin_session(identity, handoff)           # 本层只做转交
          └─► handoff.start_session(UserDraft)                     # 既有 authservice 实现
                 └─► 既有的 SessionManager.create(user_id)         # 会话在这里、也只在这里产生
```

`handoff.py` 中的 `SessionHandoff` 是一个 **Protocol**，由 authservice 实现，本层不 import `backend.auth` 的任何内部（有机械断言）。实现方必须遵守的四条：

1. **`role` / `status` 只在 INSERT 时写，绝不写在 `ON CONFLICT` 分支上**——与既有 Feishu 路径同一纪律。这样「登录」这件事本身永远无法提权或把被禁用的账号救回来。
2. **角色来自策略，不来自请求。** 草稿里的 role 只可能是 `member`，或 §5.2 授权文件匹配出的 `platform_admin`。
3. **先撤销登录前会话再签发新会话**（防会话固定），沿用既有 `SessionManager` 的既有行为。
4. **草稿里没有凭据。** 除账号自身身份外，不含 broker 回复的任何内容。

`UserDraft.to_public_identity()` 产出的字段与既有 `/auth/me`「必要身份信息」同形，**不含** `uid`、`host_id`、`login_name`、`external_id`。

Principal 绑定：平台 Principal 通过 `(host_id, uid)` 关联 Linux 账号，本地用户 id 为 `linux:<host_id>:<uid>`，**刻意不用登录名**——改名改变的是人输入的名字，不是这个人。登录名一并记录供运维阅读，但不作为标识。

---

## 8. 密码的生命周期（以及不能保证的地方）

- 口令从 HTTPS 请求体进入内存，经 `LinuxAuthService.login` → `UserProvider` → broker 客户端，被编码进一个 `bytearray` 帧，**往返结束后立即被 0 覆盖**。
- 不写文件、不写日志、不进异常、不进 URL、不进审计记录、不进命令行。
- `AuditEvent` 的字段名若含 `password` / `passwd` / `hash` / `secret` / `token` / `credential` / `shadow` 等语义，**构造即失败**——目的是让「调试时顺手记一下口令」这类事故变成响亮的报错，而不是安静的泄漏。
- **诚实的限制**：Python 的 `str` 不可变，无法被可靠擦除。从 JSON 解码得到的那一份 `str` 不在本层控制内。本层能做的是把它**复制次数与存活时间压到最低**（不存储、不格式化、不拷贝），而不是假装能擦干净。
- broker 侧：只把口令交给 PAM 会话函数一次；broker 自己的帧缓冲同样在 `finally` 中被覆盖。
- **CLI 与 Agent 不使用 Linux 密码。** 命令行工具/自动化一律使用服务端的授权 scope token（既有 token 体系），**不**使用、不需要、也不应索取任何 Linux 账号口令。本层没有、也不会提供 `--password` 参数。

---

## 9. broker 的安装（由 `alice` 执行，需要 root 审批）

> 本节描述的是**尚未执行**的动作。执行前需要 root/sudo 审批；执行人必须逐条比对，不要跳过 §10 的验证。

### 9.1 前置条件

1. 一台**已确认**的 PAM 服务名（建议 `llm-lab-portal-broker`），与主机上任何既有服务名区分开；
2. 一个**专用运行账号**或明确的调用方白名单：只有 Portal 服务进程的 UID（或专用组 GID）可以连接 socket。**不要**把 socket 开放给 `users` 之类的大组；
3. socket 目录：root 拥有、`0700`；socket 本身：root 拥有、`0600`（或 `0660` 且属专用组）；
4. §5 的两个策略文件就位并满足属主/权限要求。

### 9.2 PAM 栈

为专用服务名创建 `/etc/pam.d/<service>`（**本次实现未创建任何 `/etc/pam.d/` 条目**）：只放 `auth` 与 `account` 两类，例如 `auth required pam_unix.so` 与 `account required pam_unix.so`。

**绝对不要**在该栈中出现：

- `nullok`（会让空口令被当作正确；本实现两侧都会拒空口令，但栈本身也不该允许）；
- `session` 段里任何会起登录会话、挂载、执行命令的模块——broker **不**调用 `pam_open_session`；
- `pam_permit`、`pam_allow` 之类默认放行的模块。

### 9.3 安装方需要实现的 PAM 后端

`pam_backend.py` 目前只提供 `UnavailablePamBackend`（一律拒绝）。安装方需实现 `PamBackend` 协议（`verify(user, password) -> PamResult`），并且：

- **只**调用 `pam_start(service, user, conv, &handle)` → `pam_authenticate` → `pam_acct_mgmt` → `pam_end`；
- service 名**取自 broker 自己的 root-owned 配置**，绝不接受来自请求的 service 名（协议会因未知字段直接拒绝含 `service` 的请求）；
- 会话函数**只**从本次请求的内存缓冲取口令，来源不可是 argv、环境变量、stdin 或任何文件；
- 把 PAM 的返回值映射到 `PamOutcome`；**`PAM_SUCCESS` 之外的任何返回值都不是成功**；
- 任何异常都必须收敛为拒绝。`broker_server` 已经对后端异常做了兜底（`INTERNAL_ERROR`），但仍不应依赖它。

本次**没有**实现该后端，也没有随包提供任何 ctypes / C 互操作代码：一段无法在本机跑过一次的特权代码，看起来「已就绪」比「明确缺失」更危险。把它留成一个边界清晰的、安装时必须自行验证的实现点，是刻意的选择。

### 9.4 明确禁止

- 不创建 setuid / setgid 助手；
- 不通过 `subprocess` 调用 `su` / `login` / `unix_chkpwd`；
- 不给 Web 进程任何额外权限；
- 不把 socket 放进任何人可写的目录；
- 不为「先跑起来」而放宽 socket 权限或 PAM 栈。

---

## 10. 验收与证据纪律

### 10.1 自动化测试

```bash
python -m unittest discover -s tests/auth_linux -t .
# 294 passed
```

全部使用标准库，离线可跑（Dell 上无需 root、无第三方依赖）。覆盖：协议分帧与严格 schema、限速窗口与锁定、策略文件权限与 schema、`root`/系统账号/非登录 shell/未白名单的拒绝、空口令的两侧拒绝、broker 断连/超时/畸形回复的 fail-closed、`SO_PEERCRED` 未授权调用方**在读一个字节之前**被关闭、UID 不一致、以及**在真实 Unix socket 上**的成功与各类拒绝。

### 10.2 证据纪律（重要）

- 测试里的 PAM 是**合成后端**。合成后端返回 `authenticated`，**不能**作为「真实 `pam_unix` 会接受某个口令」的证据。这类证据只能来自在**非生产**环境、用**你自己**的测试账号、按 10.3 的步骤做的一次真实校验。
- 「HTTP 200」或「UI 显示已登录」不构成密码校验成功的证据；它们只说明上层路由不报错。
- 未验证的状态要写「UNKNOWN」，不要写「应该没问题」。

### 10.3 真实 PAM 的验证步骤（broker 安装后，由安装方执行）

1. 在一个**专用的、非人的测试账号**（例如 `llm-pam-selftest`）上设定一个**仅用于本次验证**的口令；
2. 用 broker 自己的诊断入口（或一个最小的直连客户端）在该账号上做一次成功校验与一次错误口令校验；
3. 确认失败路径返回的是拒绝而不是异常；
4. 确认 broker 的审计记录、进程日志、`/var/log/auth.log` 中**没有**出现口令明文；
5. 确认未授权调用方（换一个 UID 的连接）被拒绝且**未触发任何 PAM 调用**；
6. **删除测试账号**或将其置为非登录；
7. 把上述每一步的实际观察结果记录下来——这才是证据。

---

## 11. 回滚

本功能的默认状态就是关闭，回滚因此是「少做一件事」而不是「拆一套东西」：

1. 从 Portal 服务环境里移除 `LLM_LAB_LINUX_AUTH_ENABLED`（或设为 `0`）并重启服务。
   本层随即整体失效：`login()` 抛 `NotConfigured`，`providers()` 返回 `linux_pam: false`，UI 不再提供该入口。
2. 停止并禁用 broker 服务单元（由安装方创建，本仓库不创建），删除其 socket。
3. 视需要删除 §5 的策略文件。
4. **既有登录机制不受影响**：本次实现没有改动 `backend/auth/**`、`web/**`、`backend/portal/**`、`dev/preview_auth.py`，本地 `admin` 与 Feishu 通道保持原样。关闭本功能即回到本次改动之前的状态。
5. 已经用本路径登录过的会话：由既有 authservice 的会话机制决定其存活，本层不持有会话，无需为本层单独撤销。

---

## 12. 未完成 / 依赖他人 / 残余风险

| 项 | 状态 / 归属 |
| --- | --- |
| 真实 PAM 后端 | **未实现**，由安装方实现并验证（§9.3） |
| broker 服务单元、socket、PAM 栈 | **未创建**，需 root 审批（§9） |
| 各 Linux 账号是否有可用口令 | **UNKNOWN**，本次不得测试 |
| HTTP 路由接线（`/auth/linux/login` 等） | **不在本层**。需由 `backend/auth/**` 的所有者接线，复用既有 CSRF / 同源校验 / 会话签发 |
| `docs/auth-implementation.md` §1 的角色模型更新 | **需由该文件所有者处理**，见 §5 的声明 |
| 前端（Atelier V5）登录表单 | **不在本层**。UI 由既有的认证 UI 工作负责 |
| 策略文件属主检查在测试中 | 测试注入当前 UID；生产必须为 `0`（默认） |
| 响应耗时的微弱信号 | 已记录（§4），当前威胁模型下可接受 |
| `InMemoryThrottle` 的进程本地计数 | 已记录（§6），生产应注入 DB 限速器 |
| 多进程部署下的 broker 并发 | broker 为**串行**处理（一次一个连接），登录量极小；已加 accept 轮询与连接读超时，避免个别客户端拖住整个 broker |
| NSS 延迟 | `getpwnam` 在接入目录服务的主机上可能变慢，因此 broker 侧对连接读取设了上限 |
