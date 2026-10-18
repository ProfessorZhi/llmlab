# 本机预览的 `--state-dir` 重定向防线

本文只讲一件事：`dev/preview_auth.py::validate_state_dir` 为什么不能只查
`Path.is_symlink()`，以及它现在到底挡住了什么、没挡住什么。

相关的 `Host` 权威校验见 [`preview-host-security.md`](preview-host-security.md)。

---

## 1. 被修掉的缺陷：Windows 目录 junction

`state_dir` 是写**管理员口令哈希**与会话库的地方。原来的检查是：

```python
if path.is_symlink():
    raise errors.ConfigError(...)
```

这条在 Windows 上漏掉了目录 **junction**。junction 是 reparse point，不是 symlink：

| | `mklink /J`（junction） | `mklink`（真 symlink） |
|---|---|---|
| 需要管理员权限 | **否** | 是 |
| `Path.is_symlink()` | **False** | True |
| `os.path.islink()` | **False** | True |
| `Path.is_dir()` | True | True |
| `st_file_attributes` | `0x410` | 带 reparse 位 |

本机实测（未提权）：

```
mklink /J rc=0
link.is_symlink()      = False
os.path.islink(link)   = False
link.is_dir()          = True
link.lstat().st_file_attributes = 0x0410   # DIRECTORY | REPARSE_POINT
validate_state_dir(junction) -> 被接受
CLI 以 junction 作 --state-dir -> 正常启动并服务
```

普通目录的同一属性是 `0x0010`。也就是说：**一条只查 symlink 的防线，在 Windows 上
对 junction 完全不设防**；而 `is_symlink() is False` 会让读代码的人以为检查已经生效。

## 2. 现在的规则

`validate_state_dir` 走**整条路径**，从文件系统根一级一级到 `state_dir` 自己，
用 `lstat`（**绝不跟随**链接）逐级判定。第一个「会改向」的组件即被拒绝：

```python
def _redirect_kind(component):        # 两条判据，缺一不可
    status = component.lstat()
    if stat.S_ISLNK(status.st_mode):                     # POSIX 的机制；Windows 真 symlink
        return "symbolic link"
    if status.st_file_attributes & 0x0400:               # Windows 的 junction 等
        return "junction" if reparse_tag == MOUNT_POINT else "reparse point"
    return None
```

`st_file_attributes` 只在 Windows 上存在，用 `getattr(..., 0)` 取，POSIX 上恒为 0，
于是 POSIX 完全落到 `S_ISLNK` 那条线上——原行为不变。

错误消息点名**是哪一个组件**：

```
state_dir must not be, and must not sit behind, a link:
C:\...\junction-parent is a junction. Point --state-dir at a real directory
whose every parent is also a real directory, ...
```

## 3. 为什么要逐级，而不是只看最后一段

只看 `state_dir` 自己是不够的。把真实目录套进一个 junction，再把 state 放在里面：

```
real-parent/            <- 真目录
real-parent/state/      <- state 自己是个好端端的普通目录
junction-parent  --->  real-parent      (mklink /J)
--state-dir junction-parent\state
```

`junction-parent\state` 对**任何只看叶子**的检查来说都是普通目录，但凭据路径整体
已经被改向。逐级走一遍，才会在 `junction-parent` 这一级停下来。

## 4. 为什么不能用 `resolve()` 与原路径比较

因为本机的 `%TEMP%` 本身就是 **8.3 短名**：

```
tempfile.gettempdir() = 'C:\Users\BOB~1.EXAMPLE\AppData\Local\Temp'
Path(...).resolve()   = 'C:\Users\bob.example\AppData\Local\Temp'   # 不相等
```

用「`resolve()` 是否等于原路径」当判据，**每一个普通 TEMP 目录都会被误拒**。
8.3 短名不是 reparse point（实测 `C:\Users\BOB~1.EXAMPLE` 的属性是 `0x0010`），所以按
reparse 位判定不会误伤它。测试 `test_the_platform_temp_directory_is_never_falsely_rejected`
把这一点钉死在两种写法上。

## 5. 这个检查是什么，不是什么

**是**：纵深防御。它让「把 `--state-dir` 指向一个链接」这件事在本机预览下直接失败，
而不是静默地把凭据写到别处去。

**不是** TOCTOU 保证。检查与使用是两次系统调用，中间仍有窗口；本模块**不声称**
消除竞争。真正的边界仍然是：只绑环回、以及**绝不把生产凭据放进预览的 state 目录**。

## 6. 已知限制（有意保留，供集成者判断）

* **平台自带的改向会连带被拒**。macOS 上 `/var` 与 `/tmp` 是 symlink，
  于是它们下面的合法路径也会被拒绝。本预览不支持 macOS；Windows 与 Linux 上不存在
  这类祖先。**如果将来要支持 macOS，这里必须放宽**。
* **企业环境里被重定向的用户目录**：若 `C:\Users\<user>` 本身是 junction
  （配置文件重定向），其下的 TEMP 路径也会被拒。本机实测整条 TEMP 祖先链均为普通
  目录（`0x0010`），未出现该情形；但换一台机器需要重新确认。
* **不检查 NTFS 备用数据流、EFS、挂载点之外的 reparse 标签**：只按属性位拒绝，
  不按标签放行，所以未知标签一律被拒，不会漏。

## 7. 回归位置

* `tests/integration_auth/test_preview_http.py::StateDirRedirectTests`
  —— junction 本身、**祖先** junction、普通目录不被误拒、8.3 短名不被误拒、
  以及一条走真实 CLI 子进程的端到端用例（断言**消息里点名 junction**，
  避免把「恰好也退出 2」当成防线生效）。
* 原有的 symlink 用例 `test_the_cli_refuses_a_state_dir_that_is_a_symlink` 不变，
  消息仍包含 “symbolic link”。
