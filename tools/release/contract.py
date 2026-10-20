"""Production release contract for the same-origin llmlab portal.

This module is a **validator and planner**, not a server. It exists so that the
first production deployment can be reviewed as a data structure instead of as
a set of prose promises:

* the manifest names an exact package version and an exact 40-hex git SHA;
* the origin is loopback-only and the port must not collide with anything the
  workstation already runs;
* the mount plan has exactly three surfaces — static UI, ``/api/v2/ops`` and
  ``/auth/platform/*`` — and **no Linux authentication surface**;
* readiness is computed from explicit gates and defaults to ``NOT_READY``.
  There is no code path that turns a missing gate into ``READY``.

What this deliberately does **not** do
--------------------------------------
It never reads a secret, never talks to a database, never starts a service and
never contacts the network. The WSGI skeleton in
:mod:`tools.release.wsgi_mount` is for offline smoke tests only.

Why ``/auth/linux/*`` is refused
--------------------------------
The same-origin staging composition inherited from
``feature/v1-prod-same-origin-20261020`` wires Linux login routes through
``build_linux_service`` (``backend/bff/staging.py`` imports it and mounts
``/auth/linux/login`` and ``/auth/linux/status``). That path is PAM-shaped and
unavailable in this build. Platform accounts are a separate identity system
(Agent A, ``backend/platform_auth/``), and a release manifest that mounts the
Linux routes as *the* sign-in path is rejected here — the mismatch must be
visible in source, not discovered after rollout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "llmlab-release/1"

#: Loopback only. A release that wants a wider bind needs a different review.
ALLOWED_BIND_HOSTS = ("127.0.0.1", "::1")

#: Ports that existing Dell services or the current previews already use. A
#: candidate must not be any of them; the operator picks a free one explicitly.
RESERVED_PORTS = frozenset({
    443, 3230, 8080, 8200, 8443, 8444, 8765, 9090, 9101, 18443, 18769, 38849,
})

#: Required mount surfaces. Exactly these, and nothing that authenticates by
#: Linux account.
REQUIRED_MOUNTS = {
    "static": "/",
    "api": "/api/",
    "platform_auth": "/auth/platform/",
}
FORBIDDEN_MOUNT_PREFIXES = ("/auth/linux/", "/auth/admin/")

GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

COOKIE_SAME_SITE = ("Lax", "Strict")

#: Readiness gates. Every one is required before a deployment may claim READY.
GATES = (
    "platform_auth_verified",
    "inference_upstream_verified",
    "same_origin_verified",
    "host_deployment_approved",
    "rollback_drill_verified",
)


class ContractError(ValueError):
    """The manifest is not a valid release description."""


@dataclass(frozen=True)
class MountPlan:
    static_root: str
    api_prefix: str = "/api/"
    platform_prefix: str = "/auth/platform/"

    def resolve(self, path: str) -> str:
        """Return ``static``/``api``/``platform``/``refuse`` for a request path.

        A non-canonical path is refused here rather than classified, so no
        handler ever receives ``..`` and a later layer is not the only guard.
        """
        if not isinstance(path, str) or not path.startswith("/"):
            return "refuse"
        if "\x00" in path or "\\" in path:
            return "refuse"
        if path == "/":
            return "static"
        trimmed = path[:-1] if path.endswith("/") else path
        segments = trimmed.split("/")[1:]
        if any(segment in ("", ".", "..") for segment in segments):
            return "refuse"
        if any(path.startswith(prefix) for prefix in FORBIDDEN_MOUNT_PREFIXES):
            # Present the Linux/admin routes as absent: they are not part of the
            # platform release, and mounting them would silently imply PAM login.
            return "refuse"
        if path == self.platform_prefix.rstrip("/") or path.startswith(self.platform_prefix):
            return "platform"
        if path.startswith(self.api_prefix):
            return "api"
        return "static"


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: str
    package_name: str
    package_version: str
    git_sha: str
    bind_host: str
    port: int
    state_dir: str
    static_root: str
    trusted_proxies: tuple[str, ...] = ()
    cookie_secure: bool = True
    cookie_http_only: bool = True
    cookie_same_site: str = "Lax"
    csrf_origin_allowlist: tuple[str, ...] = ()
    static_cache_control: str = "no-store"
    data_sources: tuple[str, ...] = ()
    gates: Mapping[str, bool] = field(default_factory=dict)

    # --- validation -----------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReleaseManifest":
        if not isinstance(raw, Mapping):
            raise ContractError("manifest must be an object")
        required = ("schema_version", "package_name", "package_version", "git_sha",
                    "bind_host", "port", "state_dir", "static_root")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ContractError(f"missing manifest fields: {', '.join(missing)}")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise ContractError(f"unsupported schema_version: {raw['schema_version']!r}")
        if not isinstance(raw["package_name"], str) or not raw["package_name"].strip():
            raise ContractError("package_name must be a non-empty string")
        version = raw["package_version"]
        if not isinstance(version, str) or not SEMVER_RE.match(version):
            raise ContractError("package_version must be a semantic version")
        sha = raw["git_sha"]
        if not isinstance(sha, str) or not GIT_SHA_RE.match(sha):
            raise ContractError("git_sha must be an exact 40-hex commit")
        manifest = cls(
            schema_version=SCHEMA_VERSION,
            package_name=raw["package_name"],
            package_version=version,
            git_sha=sha,
            bind_host=raw["bind_host"],
            port=raw["port"],
            state_dir=raw["state_dir"],
            static_root=raw["static_root"],
            trusted_proxies=tuple(raw.get("trusted_proxies") or ()),
            cookie_secure=bool(raw.get("cookie_secure", True)),
            cookie_http_only=bool(raw.get("cookie_http_only", True)),
            cookie_same_site=str(raw.get("cookie_same_site", "Lax")),
            csrf_origin_allowlist=tuple(raw.get("csrf_origin_allowlist") or ()),
            static_cache_control=str(raw.get("static_cache_control", "no-store")),
            data_sources=tuple(raw.get("data_sources") or ()),
            gates=dict(raw.get("gates") or {}),
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.bind_host not in ALLOWED_BIND_HOSTS:
            raise ContractError(
                f"bind_host must be loopback ({', '.join(ALLOWED_BIND_HOSTS)}), "
                f"got {self.bind_host!r}"
            )
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise ContractError("port must be an integer")
        if not 1024 <= self.port <= 65535:
            raise ContractError("port must be an unprivileged TCP port (1024..65535)")
        if self.port in RESERVED_PORTS:
            raise ContractError(f"port {self.port} is already used by an existing service")
        if not self.state_dir or not self.state_dir.startswith("/"):
            raise ContractError("state_dir must be an absolute path")
        if not self.static_root or not self.static_root.startswith("/"):
            raise ContractError("static_root must be an absolute path")
        if not self.cookie_secure:
            raise ContractError("cookies must be Secure for a release")
        if not self.cookie_http_only:
            raise ContractError("cookies must be HttpOnly")
        if self.cookie_same_site not in COOKIE_SAME_SITE:
            raise ContractError("cookie_same_site must be Lax or Strict")
        if not self.csrf_origin_allowlist:
            raise ContractError("csrf_origin_allowlist must name the exact origin")
        for origin in self.csrf_origin_allowlist:
            if not isinstance(origin, str) or not re.match(r"^https?://[^/]+$", origin):
                raise ContractError(f"csrf origin is not an origin: {origin!r}")
        if self.static_cache_control != "no-store":
            raise ContractError("static_cache_control must be no-store")
        for proxy in self.trusted_proxies:
            if not isinstance(proxy, str) or not proxy:
                raise ContractError("trusted_proxies entries must be strings")
        for source in self.data_sources:
            lowered = str(source).lower()
            if "fixture" in lowered or "qa" == lowered or "/qa/" in lowered:
                raise ContractError("a release must not read QA fixtures")
            if lowered.startswith("/srv/") or lowered.startswith("/home/"):
                raise ContractError("a release must not read production or home paths")

    # --- derived --------------------------------------------------------

    @property
    def mount_plan(self) -> MountPlan:
        return MountPlan(static_root=self.static_root)

    def readiness(self) -> dict:
        """Compute readiness. Anything unproven keeps the answer NOT_READY."""
        results = {gate: bool(self.gates.get(gate, False)) for gate in GATES}
        missing = [gate for gate, ok in results.items() if not ok]
        return {
            "state": "READY" if not missing else "NOT_READY",
            "gates": results,
            "missing": missing,
            "git_sha": self.git_sha,
            "package": f"{self.package_name}@{self.package_version}",
            "origin": f"http://{self.bind_host}:{self.port}/",
            "note": (
                "READY here means the release gates are recorded; it is not a "
                "deployment and not a claim that the host is serving."
            ),
        }


def validate_manifest(raw: Mapping[str, Any]) -> ReleaseManifest:
    return ReleaseManifest.from_dict(raw)


__all__ = [
    "ALLOWED_BIND_HOSTS",
    "COOKIE_SAME_SITE",
    "ContractError",
    "FORBIDDEN_MOUNT_PREFIXES",
    "GATES",
    "MountPlan",
    "REQUIRED_MOUNTS",
    "RESERVED_PORTS",
    "ReleaseManifest",
    "SCHEMA_VERSION",
    "validate_manifest",
]
