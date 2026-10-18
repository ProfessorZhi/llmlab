# Dell Canary + Rollback RUNBOOK（Lane Release/QA #69, Wave-5）

> **状态：未部署。** 本文是操作手册，不是执行记录。
>
> - 东京侧已完成：产物打包、清单校验、回滚清单拒绝逻辑、dry-run 测试。
> - **Dell 侧：`HOST_PENDING`。** 没有任何 root 目录、service、Caddy 配置或端口被创建或修改。
> - **没有任何已"批准"的 Dell Portal 根目录。** 旧 root 上连 `pwd` / `git` 的 CloudCLI 权限都超时了，
>   真正的 Portal 项目授权尚未安装、也未经核验。下文 §2 的根路径是**待批准的提案**，不是既成事实。
> - 本文**不**声称真实 Dell 验证、hosted CI 通过或生产发布。

---

## 0. 本候选钉住什么（唯一候选）

| 项 | 值 |
|---|---|
| 源分支 | `integration/v1-wave4-recovered-20261017`（PR **#99**） |
| **源 commit** | `5141b1b4ae3b966bf8df7cf1dba623a90421d68b` |
| **release bundle sha256** | `04d2c286948679ca00ca4e3124ddcaf2c9f968cb81ee64769496927e979b089d` |
| bundle 文件数 / 字节 | **65** / **690,931** |
| allowlist sha256 | `74f9a1d47b66f7c3ae37d62ff052fa2c9384535e6f4345acceb63bb8652c7264` |
| 冻结 V5 | `04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf`（未变） |
| 派生 V6 | `b05e754453edfb5238249957dd1e622d452bbff57cbdf951eb7d4ee5fa7f4573` |
| **上一版（回滚目标）** | `a2dc0ee92b74fa9d480313b93f51acf7e273016ff5a017b8293f472973cf384f`（由 PR #94 的树实测重建，65 文件 / 688,289 B） |

两个 bundle digest 都是**在本机实际构建并复算**得到的，不是文档里抄来的。复算方法见 §5。

**单一候选原则**：本次 canary 只允许 `04d2c286…` 这一个 bundle。任何其它 digest 一律拒绝——
`known_good_bundles` 之外的 SHA 会被 `deploy_manifest.mjs check` 直接拒收（§6）。

---

## 1. 现有服务配置（**只读**盘点）

以下全部来自仓库文档 `docs/atelier-v6-service-hosting-and-rollback-2026-10-16.md`，
**不是**对 Dell 实机的探测结果——本 Lane 没有、也不会连到那台机器。

| 项 | 现值（文档记载） | 本 canary 是否触碰 |
|---|---|---|
| 预览单元 | `atelier-v6-preview.service` | **不触碰** |
| 专用账号 | `atelier-v6-preview` | **不触碰**（canary 用另一个账号） |
| 代码目录 | `/srv/atelier-v6/current` | **不触碰** |
| 预览端口 | `18765`（经 `Environment=PORT=` 传入，**不是** `--port`） | **不触碰**（canary 用 `18766`） |
| 状态目录 | `/var/lib/atelier-v6-preview` | **不触碰** |
| 链路口径 | 外部 TLS `:8443` → 代理 `:18443` → 预览 `127.0.0.1:18765` | **不触碰** |
| 反向代理 | Caddy（配置未入库） | **不触碰** |
| 边界 | `ProtectSystem=strict` + 仅 `ReadWritePaths=/var/lib/atelier-v6-preview`；无 `AmbientCapabilities`；不以 root 运行 | 照抄同一形状 |

> 已知陷阱（文档 §3 明确写过）：`serve.mjs` **不解析** `--host/--port`，只认 `PORT` 环境变量。
> 把 `--port 8443` 写进 `ExecStart` 会让服务静默听在 8123 上而单元仍显示 `active`。

---

## 2. 待批准的 Portal 根目录（提案）

| 项 | 提案值 |
|---|---|
| **项目根** | `/srv/atelier-v1-portal` |
| 发布目录 | `/srv/atelier-v1-portal/releases/<bundle_sha256>` |
| 当前指针 | `/srv/atelier-v1-portal/current` → `releases/<bundle_sha256>`（符号链接） |
| 状态目录 | `/var/lib/atelier-v1-portal`（与代码目录分离，且不在 git 工作树内） |
| 单元 | `atelier-v1-portal-canary.service` |
| 账号 | `atelier-v1-portal`（专用、非登录、不复用其它项目账号） |
| 端口 | `18766`，**只绑 `127.0.0.1`** |
| 服务对象 | 打包产物（纯静态），**不是** `web/` 源码树 |

**为什么不是 `web/`**：`web/` 里同时住着 `qa.html`（`data-build-profile="qa"`，`?demo=1` 能显示合成样例）、
`web/tools/**` 与 `web/README.md`。直接服务源码树会把这些一起发布。canary 只服务 §0 那个 65 文件的 bundle。

**本提案尚未获批准。** 在 §3 的全部审批门通过之前，不得创建上述任何路径。

---

## 3. 四道独立审批门（缺一不可，各自不同 owner）

| # | 门 | 谁批 | 批准前必须给出的证据 | 现状 |
|---|---|---|---|---|
| **G1** | **PAM / root / UID** | 主机 owner | canary 账号 `atelier-v1-portal` 已建、非登录、无 sudo、UID 不与其它项目共享；`/etc/pam.d` 未被本 Lane 修改 | **未批准** |
| **G2** | **Gateway / Strata** | 模型平台 owner | canary **不**改动 Gateway 或 Strata；`/api/v2/ops` 的可见性仍由 Registry 判定；未新增 key | **未批准** |
| **G3** | **Caddy / systemd / 8443** | 运维 owner | 新单元与既有 `atelier-v6-preview.service` **并存且不冲突**；`:8443` 的上游指向保持不变 | **未批准** |
| **G4** | **Owner GO** | 用户 | 维护窗口、备份可读、回滚目标 `a2dc0ee9…` 已确认存在 | **未批准** |

这四道门在 manifest 里是 `approval_gates: ["pam-root-uid","gateway-strata","caddy-systemd-8443","owner-go"]`；
`deploy_manifest.mjs check` 会逐条核对，缺一条即拒收。

---

## 4. Canary 步骤（**每一步都要先拿到 §3 对应门**）

> 全部步骤由操作者**手工**执行。本文不含可执行脚本，`tools/release/**` 里也没有任何
> `child_process` / `sudo` / `systemctl` 调用（有 dry-run 测试守着）。

1. **记录 before 状态**（只读）：既有单元是否 `active`、监听端口、`current` 指向、状态目录属主。
   一个没有 before 的回滚无法审计。
2. **取候选**：确认上面的 commit 与 bundle digest，并在地面重新构建一次 bundle，
   核对 `bundle_sha256` 与 §0 一致（不一致即停）。
3. **落盘**：把 65 个文件放进 `releases/<bundle_sha256>/`，逐文件核对 `release-manifest.json` 的 sha256。
   **不要**把 `web/` 拷进去。
4. **切指针**：`current` 指向新目录。此时**服务尚未重启**。
5. **起 canary**：按 §2 的单元形状启动（专用账号、`ProtectSystem=strict`、
   `ReadWritePaths=/var/lib/atelier-v1-portal`、`PORT=18766`、绑 `127.0.0.1`）。
   **不改**既有 `atelier-v6-preview.service`。
6. **负向验证优先**（见 §7 的真值表）：未认证访问受保护路径必须是 **401/403，不是 200**。
7. **正向验证**：canary 端口可达、页面 build profile 为 `production`、`?demo=1` 无效。
8. **记录**：写入 release record，含 SHA、时间、观察到的状态码，以及**没有**验证的东西。
9. **不要**在此时改 `:8443` 的上游。那是 G3 + 单独变更单的事。

---

## 5. 产物完整性复算（operator 在地面重复一次）

```bash
# 1) 重建 bundle 并核对 digest（不需要任何网络/特权）
node tools/release/build_static_bundle.mjs --out /tmp/atelier-v1-verify
python3 -c "import json;print(json.load(open('/tmp/atelier-v1-verify/release-manifest.json'))['bundle_sha256'])"
#   期望：04d2c286948679ca00ca4e3124ddcaf2c9f968cb81ee64769496927e979b089d
```

若与 §0 不符：**停止**。不要"先跑起来再说"。

---

## 6. 清单校验（在把任何东西放上主机之前）

```bash
node tools/release/deploy_manifest.mjs check \
  tools/release/rehearsal/canary.rehearsal.json \
  --allow-root /srv/atelier-v1-portal
```

`--allow-root` **是必填的，不是可选的收紧项**：**deny-by-default** —— 省略或给空值会拒绝
**任何** `target.root`，而不是"不限制"。根路径必须是绝对、已规范化（无尾斜杠、无 `.`/`..` 段、
无反斜杠、无控制字符）、且与给定集合**逐字相等**。

会拒收的情形（都有 dry-run 测试覆盖，其中 17 项在旧版工具上是红的）：

**根目录（fail-open 修复项）**
- 未给 `--allow-root` / 给了空值 ⇒ 拒绝一切根；
- 根不是绝对路径、带尾斜杠、含 `..`、是 `/`、或不在批准集合内。

**bundle 条目（路径逃逸修复项）**
- 条目路径是绝对路径、含 `..` 空段、含反斜杠、带 `C:` 盘符/流前缀、含控制字符、或不是 NFC；
- 条目解析后越出 bundle 目录；
- 条目路径上**任一分量是符号链接**（含中间目录）；
- bundle 根目录本身或 `release-manifest.json` 是符号链接；`check` 的清单文件本身是符号链接。

**其它既有规则**
- rollback 目标不在 `known_good_bundles` 里 ⇒ **拒绝未知 SHA**；
- rollback 目标等于候选自身；rollback 未标 `validated: true`；
- 端口落在保留集合（`8443` / `18443` / `8080` / `8123`）；
- `bind` 不是 `127.0.0.1`；
- 缺少任一审批门；`execution_authorized` 不是 `false`；
- 任何 command 形状字段（`command` / `exec` / `shell` / `sudo` / `systemctl` / `caddy` …）；
- `--out` 指向仓库工作树内部（防止覆盖源码）。

**不过度收紧**：普通相对文件名照常接受——点号、连字符、下划线与 NFC 非 ASCII（如 `图表.css`、
`V6.1_tokens.min.css`）都能通过；65 文件的真实产物是这条的回归夹具。

---

## 7. 期望状态码真值表（**只读代码事实，未在 Dell 实机验证**）

来源：`backend/bff/route_map.py`、`backend/bff/app.py`、各模块 errors。**这不是对 Dell 的断言。**

| 路由 | 未接线时 | 已接线时 | 说明 |
|---|---|---|---|
| `GET /auth/linux/status` | **200** `{enabled:false, available:false, reason:"linux_auth_not_enabled"}` | 200，且 `password_verification:"unavailable_in_this_build"` | 公开状态，不是秘密 |
| `POST /auth/linux/login` | **503** `linux_auth_not_configured`（服务缺失）／`pam_unavailable` | 200 + 会话 | 本构建**没有**真实 PAM 后端 |
| `GET /api/v2/ops` | 200（可见性由 Registry 判定） | 200 | 身份只由服务端推 |
| `GET /api/v2/ops/usage` | **503** `bff.observability_adapter_not_attached` | 200 DTO | 未接 ≠ 没数据 |
| `GET /api/v2/ops/engines` | **503** `bff.observability_adapter_not_attached` | 200 DTO | 同上 |
| `GET /api/infra/summary` | **503** `bff.host_summary_adapter_not_attached` | 200 | **不得**回一份全 UNKNOWN 的 200 |
| `GET /api/portal/services` | **503** `bff.host_summary_adapter_not_attached` | 200 | 与主机投影同一条门 |
| `GET /api/portal/capabilities` | 200（装配清单） | 200 | `ready` 不是 LIVE 声明 |
| `GET /api/atlas/calls` | **503** `atlas.source_unavailable` | 200（仅本人元数据） | |
| `GET /api/evaluation/report-sources` | 逐来源；`not_configured` ⇒ **503** | 200 | 来源隔离由评测模块自盖 |
| `GET /api/evaluation/reports` | **503** `evaluation.not_configured` | 200 | |
| `GET /api/evaluation/reports/{ref}/download` | **503** / `content_read=false` | `content_read=false` | 返回的是**判定**，不是字节；**绝不** 0 字节的 200 |

**判据**：canary 冒烟里出现「应为 503 却是 200」，按**信任边界失守**处理，立即回滚并单独报告。

---

## 8. 回滚（只在隔离的 **TEMP 假静态根** 上预演；不动任何在用服务）

预演的目的只有一个：证明"指针切回上一版"这件事可复现、且**不依赖**任何特权命令。

```bash
# 1) 一个完全隔离的临时根（不是 /srv，不是任何在用目录）
FAKE=$(mktemp -d /tmp/atelier-v1-rollback-XXXXXX)
mkdir -p "$FAKE/releases"

# 2) 用真实构建把两个版本放进假根（只读源，不碰主机）
node tools/release/build_static_bundle.mjs --out "$FAKE/releases/04d2c286…"
node tools/release/build_static_bundle.mjs --source <PR#94 的导出树> --out "$FAKE/releases/a2dc0ee9…"

# 3) 预演指针切换（纯符号链接，无特权）
ln -sfn "$FAKE/releases/04d2c286…" "$FAKE/current"
ln -sfn "$FAKE/releases/a2dc0ee9…" "$FAKE/current"   # 回滚

# 4) 校验回滚清单：目标必须是已记录的已知好版本
node tools/release/deploy_manifest.mjs check \
  tools/release/rehearsal/rollback.rehearsal.json --allow-root /srv/atelier-v1-portal

# 5) 收尾
rm -rf "$FAKE"
```

**预演的硬边界**：只在 `mktemp -d /tmp/...` 下操作；不启停任何服务；不调用 `systemctl`
或 Caddy；不写 `/srv`、`/var/lib`、`/etc`。真实回滚（在 Dell 上）仍走
`tests/release/preview-rollback-plan.txt` 的有序步骤，并需要 §3 的 G4 单独批准。

---

## 9. 本次**没有**验证的东西（不得被读成已验证）

- 真实 Dell 的任何路径、账号、单元、端口或反向代理；
- 真实 PAM 认证（本构建根本没有 PAM 后端：`/auth/linux/login` 以 503 收场）；
- 按用户的 Gateway 调用数据、Host 真相与 EvalScope 报告（全部 503 / 未接线）；
- hosted CI（配额耗尽，未执行任何 GitHub Actions）；
- 任何生产发布。

## 10. 相关文件

| 文件 | 作用 |
|---|---|
| `tools/release/build_static_bundle.mjs` | 入口闭包打包器 |
| `tools/release/deploy_manifest.mjs` | canary/rollback 清单构建与校验（拒绝未知 SHA） |
| `tools/release/rehearsal/canary.rehearsal.json` | 本候选的非生产 canary 清单 |
| `tools/release/rehearsal/rollback.rehearsal.json` | 指向上一版 bundle 的回滚清单（已预演标记） |
| `tests/release/test_v1_deploy_manifest.py` | dry-run 门禁（21 例） |
| `tests/release/preview-rollback-plan.txt` | 既有真实回滚步骤（打印型，不执行） |
| `docs/release/v1-static-bundle-gate-69.md` | 打包与 allowlist 的设计说明 |
