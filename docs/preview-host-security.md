# 本地预览的 `Host` 权威校验

本文说明 `dev/preview_auth.py`（本机环回登录预览）如何判定"这个请求是不是发给
我们这台服务器的"，以及这个判定为什么必须是**整串**的。

适用范围：`dev/preview_auth.py` 里的 `Host` 解析与 WSGI request handler。
**不适用**于生产 Portal、Caddy、Gateway 或任何反向代理——反代的行为不在本仓库
这个入口的职责范围内，预览也刻意不放在反代后面（见 §7）。

---

## 1. 被修掉的缺陷（Issue #8）

旧实现的判定只有三行：

```python
raw = str(environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "")
host = raw.rsplit(":", 1)[0] if ":" in raw else raw     # ← 只看最后一个冒号之前
host = host.strip().strip("[]").strip().lower()
if host in LOOPBACK_HOSTS:
    return None                                          # 放行
```

`rsplit(":", 1)[0]` 把字段值**截断**成冒号之前的半段，后半段从不检查。于是：

| 请求里的 `Host` | 旧判定读到的 | 结果 |
|---|---|---|
| `127.0.0.1:59008.evil.com` | `127.0.0.1` | **200，被服务** |
| `127.0.0.1:59008,evil.com` | `127.0.0.1` | **200，被服务** |
| `127.0.0.1:59008 evil.com` | `127.0.0.1` | **200，被服务** |
| `[::1]:59008.evil.com` | `::1` | **200，被服务** |
| `127.0.0.1:80` | `127.0.0.1` | **200**（端口从不比对） |
| `127.0.0.1:0`、`:abc`、`:`、`65536` | `127.0.0.1` | **200**（同上） |
| `127.0.0.1:59008` + 一行空的 `Host:` | `127.0.0.1` | **200**（重复头被逗号黏成 `…:59008,`） |

同一条规则还顺手丢掉了合法主机的端口。任何**以**环回写法开头的权威都被当成环回
权威服务。

复现证据在 `tests/integration_auth/test_preview_host.py`：整份用例在修复前的
`e0c05e0` 上跑出 58 处失败，其中 `AuthoritySuffixTests` 与
`test_a_second_empty_host_header_cannot_smuggle_a_loopback_prefix` 就是上表那两行。

---

## 2. 现在的规则

判定全部在 `_HOST_HEADER_RE` + `parse_host_authority()` 里，**输入是整个字段值**，
没有任何"取冒号前段"的步骤：

```python
_HOST_HEADER_RE = re.compile(
    r"^(?P<host>" + "|".join(_LOOPBACK_SPELLINGS) + r")(?::(?P<port>[0-9]{1,5}))?$",
    re.IGNORECASE | re.ASCII,
)
```

`Host = uri-host [ ":" port ]`（RFC 7230 §5.4），两端锚定，全串匹配：

* **允许的 hostname 只有三个写法**：`127.0.0.1`、`localhost`、`[::1]`
  （`[::1]` 带方括号，因为 `uri-host` 里的 IPv6 字面量必须带）。
* **hostname 大小写不敏感**（`uri-host` 的 `reg-name` 大小写不敏感，
  RFC 3986 §3.2.2）；靠 `re.IGNORECASE + re.ASCII` 实现，因此**只有 ASCII 字母**
  参与大小写折叠——Unicode 同形字、全角字符一律不匹配。
* **port 可选**；一旦写了，必须是 1..5 个 ASCII 数字、且落在 `1..65535`
  （`MIN_PORT`/`MAX_PORT`；范围判定与语法判定同在 `parse_host_authority()`，调用方
  无法只做一半），并且必须**等于本机实际监听的那个端口**。
* **不做任何规范化**：不做百分号解码、不做 punycode/IDNA、不把 `2130706433`、
  `0x7f000001`、`0177.0.0.1`、`127.1` 这类"另一种写法的 IPv4"折叠回 `127.0.0.1`。
  只认字面量——"先规范化再比较"会让被检查的字符串和真正被使用的字符串变成两个。
* 拒绝：多余后缀、`@`/userinfo、逗号列表、百分号编码、控制字符、内部空格、尾部点、
  多余冒号、路径/查询/片段分隔符、恶意 Unicode 与全角数字。

### 端口为什么必须匹配

"本机实际监听的端口"取自 environ 的 `SERVER_PORT`。`wsgiref` 是在 `server_bind()`
里用**已绑定 socket** 填这个值的（`setup_environ()`），不是再抄一遍客户端说的话。

* 省略端口 ⇒ 不比对（RFC 允许省略，且"是不是发给本机回环预览"由 hostname 决定）。
* 写了端口但不等于监听端口 ⇒ 400。
* environ 里没有 `SERVER_PORT`（应用被非 wsgiref 容器直接驱动，端口无从得知）⇒
  **失败关闭**，400，而不是跳过检查。

> **运维后果（已知取舍）**：把预览通过端口转发暴露出去时，
> `ssh -L 9000:127.0.0.1:59008 …` 会让浏览器发 `Host: localhost:9000`，
> 与监听端口 59008 不符 ⇒ 400。需要转发就把本地端口取成同一个数字：
> `ssh -L 59008:127.0.0.1:59008 …`。这是刻意的：端口是权威的一部分，
> 一个声明了端口却对不上的请求，本预览不回答。

### 空白

RFC 允许字段值两侧带 OWS，`wsgiref` 在写入 environ 前会 `str.strip()` 掉它们，所以
`Host:   127.0.0.1:59008  ` 会被规整后再检查（这是对的）。值**内部**的空白不被容忍：
`127.0.0.1 :59008` 一律 400。

---

## 3. 重复的 `Host`、缺失的 `Host`：为什么在 request handler 层

`wsgiref.simple_server.WSGIRequestHandler.get_environ()` 在应用运行**之前**就把
environs 摊平了，同名头用逗号拼接：

```python
env['HTTP_'+k] += ',' + v      # 两个 Host 头 ⇒ HTTP_HOST = "a,b"
```

WSGI 应用拿到的已经是一个字符串，**分辨不出到底到了几个 `Host` 头**。而 RFC 7230
§5.4 规定：一个请求带一个以上 `Host`，或 HTTP/1.1 请求不带 `Host`，都是无效请求。
所以这个结构判定必须发生在更早的地方——`PreviewRequestHandler.parse_request()`
（`dev/preview_auth.py`），直接查 `self.headers.get_all("Host")`：

* 恰好 1 个 ⇒ 放行到应用，由 `_host_guard` 判**值**；
* 0 个或多于 1 个 ⇒ 立刻 400，请求**根本不进应用**。

拒绝用的是和 WSGI 守卫同一套 400 信封（`{"ok":false,"error":{"code":"invalid_request"}}`），
并沿用 `_SECURITY_HEADERS`（`no-store`/`nosniff`/`no-referrer`），`Connection: close`。
`ThreadingWSGIServer` 与"只服务 127.0.0.1"不变。

结构判定只判"几个"，不判"是什么"：值的允许清单只有 `_host_guard` 一处，
所以走到应用的请求和没走到应用的请求，面对的是同一个判定。

---

## 4. HTTP/1.0 缺 `Host` 的政策

`wsgiref` 的 request handler 把 `protocol_version` 钉在 `HTTP/1.0`，而
`BaseHTTPRequestHandler.parse_request()` 只在
`self.protocol_version >= "HTTP/1.1"` 时才执行"HTTP/1.1 必须带 `Host`"这条检查——
于是那条检查对 **HTTP/1.1 的请求行也一样被静默跳过**（这是 Python 标准库的现状，
不是本仓库的选择）。

本预览自己把它补回来，并且**对所有请求版本**都要求恰好一个 `Host`：

* **HTTP/1.1 缺 `Host`**：RFC 7230 §5.4 明令无效 ⇒ 400，无争议。
* **HTTP/1.0 缺 `Host`**：RFC 7230 §5.4 把 `Host` 列为可选 ⇒ 严格按 RFC 可以放行。
  **本预览仍然 400（失败关闭）**，理由是：这个容器除了 `Host` 没有第二条途径知道
  自己在被谁寻址；一个无法核对的请求，就是不该被回答的请求。这一条是有意的偏离，
  写在这里而不是留给读者去猜。
* 同理，**空的 `Host:`** 也 400。

另一条**没能**保留的旧行为：不再退回 `SERVER_NAME`。`wsgiref` 用
`socket.getfqdn()` 填 `SERVER_NAME`，在 FQDN 恰好是 `localhost` 的主机上，"根本没
声明权威"的请求会被旧代码当成环回请求放行。这种失败依赖机器配置（本机
FQDN 是 `{{LAB_HOST}}`，所以在本机上它**看起来**是好的），因此
`ApplicationLevelHostGuardTests` 用手写的 environ 把它做成确定性用例。

---

## 5. 覆盖范围：三处路径一致

守卫在 `PreviewApplication.__call__` 里、**路由之前**运行，所以它对整个容器生效：

| 路径 | 处理方 |
|---|---|
| `/auth/csrf`、`/auth/**` | `backend.auth.routes.Application`（产品自己的路由） |
| `/login`、`/auth.css`、`/auth.js`、`/auth-core.js` | 静态白名单 |
| `/`、`/atlas` | 受会话保护的 DEMO |

`HostGuardAppliesToEveryRouteTests` 对这四个路径各跑一遍合法权威、前缀型权威、
外来权威、错误端口与重复 `Host`。这样就不会出现"只保护静态页"的假象。

---

## 6. 这个守卫是什么，不是什么

**是**：纵深防御。socket 已经绑在 127.0.0.1 上，一个命名了公网主机名的请求不是本
模块该回答的请求。它同时把"逗号列表""重复头"这类**同一个请求有两种读法**的形态
一律拒掉，而不是替读法做选择。

**不是**访问控制。任何能连上端口的人都可以发 `Host: 127.0.0.1:<监听端口>`。真正的
边界是**只绑环回**。这一点在"绑 127.0.0.1 不构成访问控制"的机器上尤其要记住
（例如用户态 `tailscaled` 会把打到 tailnet IP 的入站 TCP 按原端口转发给本机
`127.0.0.1`）——那里的唯一屏障是别的，不是这个头。

**改动范围**：HTTP request method 校验、chunked/请求体保护、cookie 重复名守卫、
CSRF/origin 守卫、静态面白名单全部未变。

### 已知残留（有意保留，供集成者判断）

| 形态 | 现状 | 说明 |
|---|---|---|
| `GET http://evil.com/auth/csrf HTTP/1.1` + 合法 `Host` | **404** | RFC 7230 §5.4 规定 absolute-form 的权威取自请求行。本预览不据此路由，该路径直接落到 404，**不会**被当成正常路径服务；但它也不是被 `Host` 守卫拒绝的（`HostGuardAppliesToEveryRouteTests` 之外另有 `MalformedRequestShapeTests` 固定 404 这一事实） |
| `X-Forwarded-Host` / `Forwarded` | 忽略 | 预览不在反代后面（§7）。这些头不参与判定，也不该参与 |
| 字段值两侧的 OWS（含 `\t`、`\v`、`\f`） | 被 `wsgiref` 规整后接受 | RFC 允许；值**内部**的同类字符一律 400 |
| 缺 `Host` 的 HTTP/1.0 请求 | **400** | 对 RFC 的有意偏离，见 §4 |

---

## 7. 环境前提

* 预览**不带**反向代理，也不该被放到反代后面：反代会重写 `Host`，而本模块的正
  前提就是"我看到的 `Host` 就是客户端说的 `Host`"。生产前门（Caddy → Gateway →
  Strata）与本文无关，本次改动**不碰**。
* 不产生任何外部请求：没有 Feishu、没有 Gateway、没有 Strata，测试用随机合成口令
  与临时目录，不打印任何凭据。

---

## 8. 回归位置

| 文件 | 内容 |
|---|---|
| `tests/integration_auth/test_preview_host.py` | 本主题的专项套件，共 37 例：真 socket 的 31 例（正/负权威、端口、重复/缺失 `Host`、obs-fold 与 absolute-form、非 ASCII 与控制字符、四路径一致性）+ 6 例纯函数与手写 environ 的确定性用例 |
| `tests/integration_auth/test_preview_http.py` | `RequestHardeningTests` 里固定了 Issue #8 的原始串、错误端口、重复 `Host`、无 `Host` 四例 |

在修复前的 `e0c05e0` 上，专项套件跑出 58 处失败；其中 `AuthoritySuffixTests`、
`DuplicateHostHeaderTests::test_a_second_empty_host_header_cannot_smuggle_a_loopback_prefix`、
`MalformedRequestShapeTests::test_an_obsolete_line_folded_host_is_refused` 与
`ApplicationLevelHostGuardTests` 是"新旧行为不同"的直接证据，其余是该主题的护栏。

原 `Host` 用例（非环回 → 400；`127.0.0.1` / `[::1]` / `localhost` 放行）保持原样，
仍然是绿的。
