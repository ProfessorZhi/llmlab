# Phase 4 — final acceptance (§10)

What §10 of the brief asks for, read clause by clause against what was measured,
with the clauses that are **not** met stated as plainly as the ones that are.

§10 reads: *"Final acceptance — real Dell deployment + access test; Windows runs
no Portal; Dell independently serves the whole Web platform; authenticated
browser access over HTTPS; Strata unchanged and running; EvalScope/Grafana
independently controllable; Prometheus really sampling and storing history;
Benchmark and report centre usable; service behaviour after reboot or SSH logout
consistent with the actual management mechanism; frontend dark/light and mobile
correct; management actions not triggerable by unauthorized users; GitHub private
repo and PR established; all new components rollback-able."*

Readings dated 2026-10-10 were taken before the redeploy; readings dated
2026-10-11 were taken after it, on the deployed tree. Which is which is stated
per clause rather than averaged.

Status: **eleven of thirteen met, one partial (blocked on a human, by design),
one open and reported as its own item.**

---

## 1. Clause by clause

| # | Clause | Verdict | What was read |
|---|---|---|---|
| 1 | Real Dell deployment + access test | **met** | The §8 tree is live: `40-deploy-code.sh` PASS at commit `334597f`, health check `REVISION OK: unified-entry`, and the portal answers `200` on its own loopback with no workstation in the path (§2.1). |
| 2 | Windows runs no Portal | **met** | Zero llm-lab processes on the workstation; `8765` and `8791` have no listener. What was stopped, and the one unplanned casualty, are in §4. |
| 3 | Dell independently serves the whole Web platform | **met** | All six services `up` (§2.1). |
| 4 | Authenticated browser access over HTTPS | **partial — blocked on the human step the brief asks for** | TLS chain verifies against the node's own CA, `54-accept-caddy.sh` 42 checks PASS, and the CA is installed in `CurrentUser\Root` so a browser can validate it without an override. Passing the basic-auth prompt is left to the operator on purpose (§5.1). |
| 5 | Strata unchanged and running | **met** | `model_api.ok=true`, `loaded=true`, `auth_required=true`; GPU `available=true`. Untouched across the redeploy; `71-accept-monitoring.sh` confirms nothing in the chain opens a file in Strata's tree. |
| 6 | EvalScope / Grafana independently controllable | **met** | `labctl {start,stop,enable,disable}`; `71-accept-monitoring.sh` PASS; `54` proves even an admin cannot stop Caddy from the console. |
| 7 | Prometheus really sampling and storing history | **met** | Measured, not inferred — see §2.2. |
| 8 | Benchmark and report centre usable | **met** | On the deployed tree: the newest run (`2026-10-10_iq2xs_lowram_32k`) opens with 15 files, and **every** listed report previews and downloads (2/2), as a round-trip through the paths the listing publishes. |
| 9 | Behaviour after reboot / logout matches the real mechanism | **met, with the stated caveat** | `@reboot` cron + a five-minute tick, and it is labelled *not systemd* in the service's own docs (phase 3 §6). |
| 10 | Frontend dark/light and mobile correct | **met** | 60 renders × 2 themes, 0 problems — and the audited front end is byte-identical to the deployed one: `git diff --stat ec989e0 334597f -- portal/web/` is **empty**. The claim travels with the deploy because the bytes do. |
| 11 | Management actions not triggerable by unauthorised users | **met** | `54-accept-caddy.sh`, 42 checks PASS: no credentials → 401; *viewer* refused on `/api/service/*`; even *admin* cannot stop Caddy from the console. |
| 12 | GitHub private repo and PR established | **met** | `ProfessorZhi/llm-lab`, private, PR #1 **OPEN / MERGEABLE / CLEAN**. Its last code commit `334597f` is the revision running on the node; the only commit above it is this record. Not merged, as the brief requires. |
| 13 | All new components rollback-able | **met, and exercised** | See §2.3. |

**Open as its own item — not a failure of this deploy:** the EvalScope Dashboard
is still not published through the unified HTTPS front door. `/api/lab` reports
it as `via=not-exposed`, `url=""`, because its build emits root-absolute asset
paths that a sub-path proxy cannot serve. It must not be counted among the
native workspaces a browser can reach, and this record does not count it.

## 2. The readings behind the "met" rows

### 2.1 The node serves itself

```
$ deploy/templates/remote.sh -c '/usr/bin/python3 …/labctl.py status'
portal           up  8765  287111  绑 localhost；局域网访问一律经反向代理。
caddy            up  8443  220376  局域网唯一入口：TLS + 认证 + 按路由分权；控制台不能启停它。
evalscope        up  9000  212682  独立 venv（CPython 3.12）；不触碰 Strata 的 Python
grafana          up  3000  220400  独立于 Strata 的进程；停掉它不影响推理。
metrics-adapter  up  9101  212774  把 Strata 的 JSON /metrics 译成 Prometheus
prometheus       up  9090  209780  采样与留存都有上限：30 天 / 5 GB，先到先失效。
```

`/api/v2/live` from the node returns live Strata health: model
`qwen3.8-flash-next-iq2_xs`, `loaded=true`, RTX A6000 with cross-checked memory
telemetry (metrics `40050.6 MiB` vs status `40051.0 MiB`, delta 0.0 %). The
workstation appears nowhere in that path.

The navigation the deployed tree publishes, from `/api/lab`:

| group | items | how it is reached |
|---|---|---|
| 本 Portal | 总览 / 模型与聊天 / 评测中心 / 计算与监控 / 报告中心 / 设置与集成 | the page itself |
| Strata 原生界面 | Chat / Monitor / About — **one** product, three views | `https://{{LAB_HOST}}:8443/strata/` |
| EvalScope | Dashboard | `not-exposed` (§1, open item) |
| Grafana | Grafana | `https://{{LAB_HOST}}:8443/grafana/` |

### 2.2 Prometheus is sampling, not merely running

A `up` query is the narrowest honest test — a process can be up with an empty
TSDB, and an empty TSDB is not history. Re-measured on 2026-10-11 through the
whole chain:

```
$ 71-accept-monitoring.sh
  ok   Prometheus holds the adapter's GPU memory reading  -- prometheus=41996058624.0 adapter=41996058624.0
  ok   a query through Grafana returns the same reading as the adapter  -- grafana=41996058624 adapter=41996058624.0
  ok   Prometheus runs with a retention limit  -- 30d or 5GiB
```

The value travelled `Strata JSON /metrics → metrics-adapter → Prometheus →
Grafana` and arrived unchanged. That is a stronger reading than the 2026-10-10
one, which stopped at Prometheus.

### 2.3 Rollback — and now the proof that it runs

* The deploy keeps the previous tree: `…/llm-lab.prev` holds the pre-§8 build
  right now, so reverting is one swap plus one restart.
* `deploy/templates/node/40-deploy-code.sh` gained an automatic rollback, and it is
  not a claim: a sandbox harness runs the real script against a fake portal that
  reproduces the property that makes this hard — front end read from disk per
  request, back end fixed at import. **S1** settles with the new tree live and
  the old one kept; **S2**, a restart that reports success and replaces nothing,
  is caught by the revision check and rolled back; **S3**, a build that dies on
  startup, is caught by the baseline and rolled back. 28 checks, all passing.
* `labctl stop|disable` is per service; Caddy is `portal_managed = false`
  precisely so the console cannot take away the door it is being read through.

## 3. The redeploy, and why it needed three fixes first

The clause that was gating everything was clause 1: the node served the pre-§8
tree, so clauses 8 and 10 could only be claimed against the workstation build.
Closing it meant changing code on a running service, which the brief authorised
under a scoped change permit — *"允许更新 Dell 上的 LLM Lab Portal 代码，并对
LLM Lab Portal 执行必要的受控重启"* — subject to three review findings being
resolved first. All three are implemented and behaviourally tested:

1. **`labctl` had no `restart`.** Signalling and relaunching in one breath races
   the kernel: the old process may not have released its socket, the new one
   fails to bind, and the service ends up down in a way that reads as "the new
   build is broken". `restart` waits for the process to actually exit — for a
   zombie, which `kill(pid, 0)` still reports alive, it reads the kernel state
   field — and for the port to actually come free, and refuses to launch
   anything until both hold.
2. **`stop_service()` refused only a *known* pid reuse.** Every other way of
   losing the identity record fell through to a kill. The gate is now inverted:
   the portal must prove the pid is the one it started, from the kernel's own
   start time, and otherwise signal nothing.
3. **`40-deploy-code.sh` left `.prev` for a human.** It now verifies its own
   work — asking the new process a question only the new code can answer — and
   on any failure restores the previous tree and re-checks, enforced by an EXIT
   trap on paths nobody wrote.

The deploy itself:

```
$ git archive … --add-file=.deployed-commit 334597f  →  llm-lab-code-334597f….tar
   140 entries, no credential-shaped paths
$ remote.sh --put … /tmp/llm-lab-code.tar
   sha256 095d63ef…d0f6  (node copy identical)
$ remote.sh deploy/templates/node/40-deploy-code.sh     →  exit 0
   previous tree kept at {{LAB_ROOT}}/llm-lab.prev
   == stop portal   pid 223466 → exited, localhost:8765 free
   == start portal  pid 287111 → listening on localhost:8765
   REVISION OK: unified-entry   (6 service rows, each with state and identity_ok)
   deployed: commit 334597ff9f2fc28e1b1641279cd97cd598faebdd
   VERDICT: PASS
```

The archive was built from the **confirmed PR SHA**, not from the working tree,
so no uncommitted code could enter it; the `.deployed-commit` marker inside is
what lets the node say which revision it is serving.

Note what the old labctl proves: it has **no** `restart` subcommand, so the
deploy's own fallback path (`stop` / wait-for-port / `start`) is the one that
had to work on the real rollback route. It was written for exactly this and is
now exercised rather than assumed.

Acceptance on the deployed tree: **`70-accept-portal.sh` PASS**,
**`54-accept-caddy.sh` PASS (42 checks)**, **`71-accept-monitoring.sh` PASS**.

## 4. Windows: what was running, and what was stopped

The acceptance criterion is "Windows 只通过浏览器安全访问". On 2026-10-11 the
workstation had **three** portal processes, not two:

| pid | command | port |
|---|---|---|
| 32552 → 30444 | `…/evaluation/.venv/Scripts/python.exe -u portal\server.py` → `python.exe -u portal\server.py` | **listening** on 8765, and holding the browser's 4 established connections |
| 33696 | `python portal/server.py --port 8799` | **also** listening on 8765 |
| 15156 | `python portal/server.py` (via `nohup`) | 8791 |

The duplicate bind is the defect the earlier record described, and the socket
read makes it concrete: the *listener* for new connections was 33696, while the
browser's live connections belonged to 30444. A browser's page and a browser's
next request were being served by different processes, non-deterministically —
which is precisely why a status code from this workstation proved nothing.

All three were confirmed to be this project's own `portal/server.py` before
being stopped, and no scheduled task or startup entry relaunches them (checked,
because "nothing is running" is worthless if something puts it back). The
workstation now reports **zero** llm-lab processes and no listener on 8765/8791.

One casualty, reported rather than smoothed over: `taskkill /T` on 30444 walked
its child tree and took the **workstation's** EvalScope service with it
(`evalscope.exe service --port 9000`, pids 19284/12192), because that portal had
launched it. It is not a loss — the node runs its own EvalScope (`up`, pid
212682) and the goal is that the workstation runs no LLM Lab background service
at all — but it was a side effect of the tree kill, not a decision, and it is
recorded as such. Two stale headless Edge screenshot processes aimed at
`localhost:8765` were cleared in the same pass.

**The node was not touched by any of this.** Its six service pids either side of
the cleanup are identical.

## 5. What is blocked, and what is open

### 5.1 The browser leg through the front door — waiting on the operator

Everything up to the login prompt is verified, and the certificate question is
settled the honest way rather than worked around:

```
CN=Caddy Local Authority - 2026 ECC Root
sha1 B1:3A:56:A8:40:9F:6E:42:28:04:BF:8A:B7:DF:7B:F5:08:3D:26:08
notAfter 2036-08-18
⇒ installed into Cert:\CurrentUser\Root (user scope, no elevation, removable with
  `certutil -delstore -user Root <thumbprint>`)
```

The node's own `54-accept-caddy.sh` verifies the chain against this root with a
real verifier and returns 42/42, so the browser's trust anchor is the same one
the node signed with. What is deliberately **not** done: clicking through a
warning, or disabling certificate checking. The brief forbids both, and the CA
install is what makes neither necessary.

Past the login prompt needs the `admin` or `viewer` password, which lives at
`{{LAB_ROOT}}/services/secrets/caddy-auth.env` on the node, mode 600. §十
forbids reading, printing or committing it, and the operator has asked to log in
manually — so the browser leg stops here and waits rather than being accelerated
by a workaround.

### 5.2 The EvalScope Dashboard is still not published

Restated here so it is not lost: the third native workspace is reachable on the
node but not through the unified front door. Until its asset paths are made
sub-path-relative (or it is fronted at a root), a browser cannot open it, and
no part of this record should be read as saying otherwise.

## 6. What this record does not claim

* It does not claim a browser has been driven past the login prompt. §5.1 says
  why, and the reason is the brief's, not an omission.
* It does not claim the EvalScope Dashboard is browser-reachable. §5.2.
* It does not claim every clause was measured on the same day. The pre-deploy
  readings are dated 2026-10-10 and the post-deploy ones 2026-10-11, and each row
  says which it is.
* It does not claim the workstation's EvalScope was removed on purpose. §4.
