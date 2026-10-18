"""Regenerate the cross-language fixture the browser contract is checked against.

    python3 tests/bff/make_golden.py

The fixture is produced by the **real** BFF request path — login over WSGI, a
real session, a real ``GET /api/v2/ops`` — with two things pinned so the output
is reproducible:

* the Registry's clock, fixed at one instant (timestamps would otherwise move);
* the Registry's id factory, replaced by a counter (ids come from
  ``secrets.token_hex``, which is right in production and fatal to a byte-level
  comparison).

The admin payload is produced by a session that has really entered admin mode
over ``POST /auth/admin/enter``, with a synthetic shared password. Nothing from
the elevation itself appears in the payload — the payload shape is unchanged —
but the *view* it produces is the admin one, which is the half the browser
contract needs to keep seeing.

Pinning the inputs rather than post-processing the output is what makes the
fixture worth comparing against: it is a genuine product of the code under test.

``tests/web-features/ops-bff-payload.test.js`` then feeds the same file to the
browser's own ``validateOps`` and renderer, which is the only check that proves
the server and the front end agree about the shape.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.bff import support as S  # noqa: E402

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "web-features"
    / "fixtures"
    / "bff-ops-payloads.json"
)


def generate() -> dict:
    """Produce the three payloads the fixture holds."""
    with S.deterministic_ids():
        harness = S.build_harness(
            state_dir=tempfile.mkdtemp(), clock=S.FixedClock()
        )
        try:
            ids = S.register_baseline(harness, "alice")
            S.share(
                harness.client("alice"),
                ids["deployment_id"],
                mode="selected",
                principals=["4003"],
            )
            # One queued evaluation run, so the fixture covers the evaluation
            # section too — queued and never dispatched, which is the only
            # outcome this deployment can produce.
            harness.submit_eval("alice", ids["deployment_id"])
            payloads = {
                # The owner's view: one owned deployment, now shared.
                "member_private": S.ops_of(S.fetch_ops(harness.client("alice"))),
                # The grantee's view: the endpoint that was shared *to* them.
                "grantee_shared": S.ops_of(
                    S.fetch_ops(harness.client("bob"), "shared")
                ),
                # The administrator's view: the deployment-wide list. Reached by
                # entering admin mode, because under the V6 model `carol`'s plain
                # session is a member like everyone else's — the fixture has to
                # keep covering the admin shape, so it elevates a session to get
                # there rather than assuming an account is enough.
                "admin_private": S.ops_of(S.fetch_ops(harness.admin_client("carol"))),
            }
        finally:
            harness.close()
    return payloads


def main() -> int:
    payloads = generate()
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        json.dumps(payloads, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE.relative_to(FIXTURE.parents[3])}")
    for name, payload in payloads.items():
        owned = len(payload["registry"]["owned"])
        shared = len(payload["registry"]["shared"])
        metrics = len(payload["metrics"]["items"])
        series = len((payload["trends"] or {}).get("series") or [])
        runs = len(payload["evaluation"]["runs"])
        print(
            f"  {name:16s} owned={owned} shared={shared} "
            f"metrics={metrics} series={series} runs={runs}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
