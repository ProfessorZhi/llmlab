# Legacy Windows tools

Scripts from `scripts/` in the private `llm-lab` repository, kept as reference
for workstation-side operations.

| File | Origin | Purpose |
|---|---|---|
| `bootstrap_remote_access.py` | `scripts/bootstrap_remote_access.py` | One-time setup of SSH access to the inference node. |
| `check-env.ps1` | `scripts/check-env.ps1` | Environment preflight. |
| `selftest.py` | `scripts/selftest.py` | End-to-end self-test against the node. |
| `start.ps1` / `stop.ps1` | `scripts/start.ps1`, `scripts/stop.ps1` | Start / stop the local console process. |

## Warning

`bootstrap_remote_access.py` deals with **credential entry**: in the original
repository it could accept an SSH password on the command line and write an SSH
key. It has been left byte-for-byte functionally intact apart from sanitization,
so read it before running it:

* it targets paths that are now `{{LAB_*}}` placeholders, and
* it is not used by the new portal and is not part of any automated flow.

Do not pass secrets as command-line arguments on a shared machine, and do not
add this script to CI.

Loophole addresses were rewritten to `localhost`; the node address appears as
`{{LAB_HOST}}`. No key, password or fingerprint is present in this directory.
