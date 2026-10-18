# V1 identity, authorization and security design

> Target state, not evidence that PAM, production step-up or the Gateway enforcement path are deployed. Decisions that change system accounts or root-owned policy require explicit operator approval. Also read [existing ADR](../integrations/ADR_LINUX_IDENTITY_AND_ADMIN.md).

## 1. Identity and sessions

- The Login screen takes each person's **own** Linux username and password; current known identities are `alice`, `carol`, `bob`. A session resolves to immutable `principal=linux:<host_id>:<uid>` through a restricted PAM Broker. Usernames are display data, not authorization keys.
- TLS/controlled ingress → same-origin BFF → restricted broker Unix socket; no browser direct access to PAM, Linux `/etc/shadow`, model files or source ports. Broker returns authenticated ID only; passwords are never persisted, logged or pasted into ChatGPT/GitHub.
- Auth service issues HttpOnly / Secure / SameSite session cookie, CSRF proof, explicit expiry; logout, account disable, expiry or policy revocation denies the **next** protected request. Logout revokes and clears the cookie even when the session is already expired or the account is disabled; only a request with no session cookie at all is `401`. Every account's session cache and personal frontend state are isolated.
- Identity comes only from the server-side session. On IAM-owned auth routes and `/auth/linux/login`, a `uid` / `user_id` / `principal` / `owner` / `role` field in the query or body refuses the whole request (`400 identity_assertion_rejected`) rather than being silently ignored. BFF-only routes keep their own field policy and are a BFF handoff.
- CLI and SDK use separately issued scoped API tokens, safely hashed at rest; plaintext revealed only once and never shown in logs. Expiry/rotation/revocation effective next request. Web temporary admin state is per-session and must not upgrade tokens: a token minted from an elevated member session still carries `member` and the narrow default scope set.
- If the PAM Broker isn't actually installed or authorized, login reports `pam_unavailable`; must not offer working fake login.

## 2. Resource authorization

- **Member**: authenticated principal; may access only its own and explicitly shared resources. Owner of an Endpoint can register/verify/share/revoke and query own metering when backend confirms it.
- **Owner**: object-level role; does not imply system administrator. Endpoint default Private; resource identity and grants checked on **every** read/write/invoke.
- **Temporary platform administrator**: a per-session, limited-time escalation (design ceiling 15 min) after approved eligibility **and** verified per-user step-up. It grants only enumerated Portal scopes, e.g. global sanitized host monitoring / operator event summaries. No Linux root, shadow files, arbitrary commands, raw other-user prompts, another Owner's model write or background GPU control.
- **Admin eligibility**: Recommended allowlist of explicitly approved immutable (host_id,uid,login) records, **not** “every Linux user is admin.” `alice`, `bob`, `carol` may all be explicitly eligible if product owner approves; ordinary login still stays Member. Production use of a shared admin password is not preapproved. Secondary factor method is an open approval item.
- **System operator**: human with actual operating-system privilege outside the Portal and a separate change-control process; do not equate to a Portal admin role.

## 3. Required security gates

| Boundary | Rule | Critical negative test |
| --- | --- | --- |
| Login / Broker | reject root/system/locked accounts; bind host+UID; same-origin/CSRF and throttling | spoofed `uid`, `role`, `owner` cannot alter identity |
| Session / Cache | TTL, logout invalidation, user-switch clear, no private-data flash | A logs out → B logs in cannot see A's cached calls |
| Registry | Owner/ACL per object, hidden resources indistinguishable | B cannot enumerate private A Endpoint or force share |
| Gateway | caller token introspection, current grant, applied Router revision | A revokes B → B next live request denied |
| Calls / Atlas | trusted key→principal mapping, filter before aggregate | B cannot infer A volume/tokens or error count |
| Evaluation | audited report Owner and source, bounded file references | unresolved owner report inaccessible even to admin-read |
| Admin | per UID step-up, expires/exit/revoke; no privilege inheritance | A session elevated ≠ another tab/device or CLI token |
| Operations | fixed allowlisted metrics/probes/diagnostic events | arbitrary PromQL, URL, file path, command rejected |
| Credentials | redaction, least retention and no secret display | password/token/key/prompt absent from logs, PR and screenshots |

Every write requires correct session or scoped token, per-route capability, CSRF/origin (for browser), target Owner ACL and an auditable result. Disallow cross-UID access even if frontend hides a control: backend is authoritative.

## 4. Security status and decisions

**To implement offline:** auth DTO/state machines, browser disabled states, safe cookie policies, broker adapter interfaces, ACL checks, audit schemas, fake-principal rejection tests and bounded token paths.

**Requires host operator approval:** deploy/permission a real PAM Broker, edit root-owned eligibility or policy files, change Linux accounts/passwords, production step-up factor, Gateway effective authorization configuration, ingress/systemd changes.

**Open decision D-IAM-01:** exact admin-eligible UID list and allowed platform READ scopes. **D-IAM-02:** independent production step-up method and account recovery. **D-IAM-03:** whether any platform MANAGE scopes are genuinely needed for V1 (default no). Record decisions as reviewed ADR changes before enabling endpoints.
