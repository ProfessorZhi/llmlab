# EvalScope installer (legacy)

The private `llm-lab` repository installed EvalScope into a project-local
virtual environment on a Windows workstation. Both variants are kept here:

| File | Platform |
|---|---|
| `install-evalscope.ps1` | Windows (PowerShell 5.1+), original |
| `install-evalscope.sh` | Linux/macOS equivalent, added during consolidation |

Both create the virtual environment under the checkout (`evaluation/.venv`),
never touch the global interpreter, never elevate, and default to the Tsinghua
PyPI mirror. Override the index with `-IndexUrl` (PowerShell) or `--index-url`
(shell).

## Status

Reference only. Not invoked by the portal, CI or any service unit.

## Sanitization

No host, path or credential is embedded: the checkout root is the parent of the
repository checkout, and no download URL other than the package index is used.
