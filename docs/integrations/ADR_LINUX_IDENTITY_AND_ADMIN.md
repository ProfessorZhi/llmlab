# ADR · Linux 用户身份、资源 Owner 与临时平台管理员

- **Status: PROPOSED / REQUIRES PRODUCT OWNER DECISION BEFORE PRODUCTION**
- **Date:** 2026-10-17
- **Existing sources:** [Linux PAM design](../linux-pam-login.md) · [V6 shared administrator mode](../v6-shared-admin-mode.md) · [V6 target architecture](../architecture.md)
- **Current implementation reality:** `backend/auth_linux/pam_backend.py` has an unavailable/fail-closed backend; the root-owned production broker and actual PAM stack **have not been installed**. The existing shared-admin-mode design explicitly says **non-production only**. No real Linux password has been verified by this document.

## 1. The business question: "Can every Linux user log in as administrator?"

**The answer should be: they log in as themselves, and approved users may temporarily enter administrator mode.** Not a second shared `admin` login; not signing in as `root`; not a shared Linux account.

We need to separate **four orthogonal ideas**:

1. **Authentication:** Linux username/password checked via a protected PAM Broker; yields a stable `linux:<host_id>:<uid>` Principal. Every action remains attributable to that person's UID.
2. **Resource Owner:** A user may create, verify, share and revoke **their own** Engine / Deployment / Endpoint, without being a platform admin. "Owner" is an object relationship, not a global user role.
3. **Platform administration (UI/business scope):** time-bounded, explicitly eligible step-up of the current authenticated session. Primarily platform-wide **metadata/health/audit read**; separate manage capabilities by scope.
4. **Infrastructure root/operator:** maintains PAM files, systemd, Caddy, Strata, GPU allocation and physical data protection through a separate host operations change process. A Portal administrator never inherits these permissions.

### Ordinary member / resource owner permissions

| Feature | Own / explicitly shared assets | Global platform admin required? |
| --- | --- | --- |
| See own models, own Endpoint IDs, shared-to-me endpoints | yes, based on Registry ACL | **No** |
| Register/verify own Engine/Model/Deployment/Endpoint, if approved by policy | yes, within owner scope and safe quotas | **No** |
| Share/revoke one's own Endpoint | yes, after real ACL and Router checks | **No** |
| Issue/revoke own scoped API tokens | yes, once Token Authority and Session verified | **No** |
| Invoke permitted Endpoint and see one's own request metadata | yes | **No** |
| View actual GPU host summary | depends on explicit `observability:host:read` policy | **No automatic inference** |
| See global traffic and other users' metadata | only if explicit per-scope admin read permits it | **Yes, narrowly** |
| Read others' prompt text, tokens or personal reports | **No** by default | **Not unlocked by admin read** |
| Kill other users' Engine / edit PAM / read HOME / write Gateway config | **No** | **Not a Portal admin capability** |

## 2. Two available policy approaches

| Question | Option A: all approved members eligible | Option B: named eligible admins |
| --- | --- | --- |
| Who may request temporary admin mode? | Every user on the approved Portal Linux whitelist | Only explicit, root-owned `(host_id,uid,login)` entries |
| Is ordinary login admin? | **No** | **No** |
| Does eligibility itself grant global scope? | **No**, requires per-session step-up | **No**, requires per-session step-up |
| Abuse/leaked factor impact | Larger eligible set; shared factor especially risky | Smaller eligible set; revocable individually |
| Audit attribution | Original UID always present | Original UID always present |
| Suitability | Small trusted group if explicitly selected by owner | **Recommended production default** |

**Recommended:** Option B. If the product owner wants `alice`, `bob` and `carol` all to be eligible, give *those three identities* explicit eligibility; they still need their own Linux credentials + a fresh step-up and keep separate audit entries. Do not silently add current or future Linux accounts to the admin list.

**Product decision requested:** should all allowlisted members be eligible (A), or should `alice`/`bob`/`carol` be individually approved (B), and which global **read** abilities should be included? Implementation must not guess on the user's behalf.

## 3. Production step-up: reconcile the two existing designs

Two reviewed but not yet production-consistent documents exist:

- `docs/linux-pam-login.md` defines a **root-owned** platform-admin grant file and Linux-login-derived underlying role. This is an *eligibility/host policy* fact, **not sufficient** to elevate a Portal session.
- `docs/v6-shared-admin-mode.md` implements session-bound admin elevation (≤15m) guarded by a shared independent administrator password and Linux-PAM-sourced Session, even for a user whose underlying record says `platform_admin`. It explicitly applies to **non-production** environments.

**Required reconciliation:**

```text
Linux HTTPS login (own name + password)
    └── restricted PAM Broker -> authenticated (host_id, uid)
          └── ordinary Session: member scopes + owned/shared object ACL
                └── admin eligibility policy (operator-approved)
                      └── fresh user-bound step-up proof
                            └── session elevation, TTL <= 900 seconds
                                  └── specifically granted ADMIN_READ scopes
                                        └── every request rechecks: Session, TTL, policy, ACL
                                              └── exit/timeout/logout/revoke -> member immediately
```

- **No generic shared Linux-admin identity** and no `root` HTTP login.
- The existing shared admin password may be retained **only for nonproduction tests** unless a production threat-model ADR explicitly authorizes it. A global shared password does not provide an independent, per-user second factor; it can be copied among users, making fine-grained eligibility enforcement weak.
- **Preferred production step-up:** a **per-user** additional factor or fresh broker reauthentication bound to the same `(host_id,uid)`, session and device with short TTL; the actual factor (per-user password recheck, WebAuthn, TOTP, or locally managed equivalent) requires its own implementation/security review. No claim that WebAuthn/TOTP currently exists in this repo.
- User identity credential is only submitted over protected same-origin HTTPS to the limited login handler and transiently checked by the Broker. The BFF must not store Linux passwords or log them; no GUI root password is requested.
- Prevent shared-credential escalation: attacker with another member's session or CSRF cannot elevate merely by setting `role=admin`, `is_admin`, `X-Admin` or calling `/auth/admin/enter` without current authenticated session + policy + step-up.
- Multi-tab/multi-device semantics are **per Session**, not "this person is admin everywhere." Logout / credential policy rotation / expiry immediately revokes elevation. If a user loses admin eligibility, next protected request must be denied.
- For write actions: admin read **does not** imply admin manage. E.g. `registry:admin:manage`, `evaluation:dispatch:real`, production Gateway router control and Linux system operations remain separate explicit approvals/policies.
- Admin audit events must retain original Principal, UID, session hash, time, action, target, result, step-up start/end and reason without recording any factor/token/secret.

## 4. Access matrix for acceptance tests

| Scenario | Member session | Temporarily elevated authorized admin |
| --- | --- | --- |
| Read your own calls | permitted when self scope | same |
| Read somebody else's calls | denied | only if separate narrowly approved admin metadata scope; raw content still blocked |
| View own and shared Endpoint | permitted | same |
| Read all Endpoint metadata | denied | may be permitted by explicit global admin read scope |
| Share/revoke somebody else's Endpoint | denied | **still denied** unless separately explicit manage/delegation scope |
| Read platform host GPU metrics | based on host-read scope | based on host-read scope; not automatic root |
| View platform audit summary | denied | permitted only with audit read scope |
| Obtain Linux shadow files, private model weights, API key plaintext | denied | denied |
| Start/stop Gateway or other user Engine | denied | denied pending separate operator approval |
| Step-up with a shared admin account | not available | not available; always same Linux UID |
| Session expiry, logout, policy revoke | immediately deny protected operations | immediately downgrade/deny |

**High-priority negative tests:**
`alice` regular Session still member even when root-owned file marks admin eligibility;
`bob` user cannot use an arbitrary `platform_admin` string to see `carol` data;
two separate Sessions for the same user do not share elevation;
expired/revoked admin elevation reverts at next request;
token use via CLI does not inherit ephemeral browser admin elevation;
all operations still enforce object Owner/ACL.

## 5. What to show in the UI now

- Login screen: Linux username + password, provider `linux_pam` only when broker configured; otherwise a truthful "PAM not configured" state, no default admin password.
- Top-right current identity: display name, current Linux Principal context when allowed, Session expiry, "member" or "temporary admin" status with remaining TTL.
- "Enter administrator mode" button: only available if **this Session** meets real eligibility and the production step-up method is configured; otherwise disabled with stable reason. No fake success animation.
- Administration page: approved read-only audit/health + explicit elevated scope list; write operations disabled until separately admitted.
- Ordinary users may still fully manage **their own** models and permitted Endpoints from Models/API pages without using administrator mode.

## 6. Decision log — must be answered before the real IAM deployment

- [ ] Who is admin-eligible? **Option A** (all whitelist) vs **Option B** (named identities), with exact identities approved.
- [ ] Production step-up factor and credential lifecycle; do not transplant shared nonprod password silently.
- [ ] Which global **read** scopes? Host telemetry, API metadata, member directory, Gateway utilization, audit summary; data-minimization constraints.
- [ ] Does production need any **manage** scope, and for what exact assets, conditions, audit and rollback?
- [ ] Revocation/rotation behavior across Sessions, CLI scoped tokens and stale caches.
- [ ] Real PAM broker owner, UNIX socket policy, deploy/rollback and security test execution approved by system operator.

**No action is taken by this ADR** to create Linux accounts, set passwords, install PAM, change root grants or change the current running preview.
