# Legacy gateway material

Three gateway variants from the private `llm-lab` repository, kept for reference
while the integrated gateway is designed. **Nothing here is wired into the
portal and none of these is a production default.**

| Directory | Legacy source | Summary |
|---|---|---|
| `pr-02-single-gateway/` | PR #2 | One upstream, a key store, per-key limits and Prometheus telemetry. The smallest complete OpenAI-compatible gateway (`gateway.py`, `keystore.py`, `limits.py`, `admin.py`, `telemetry.py`, `selftest.py`). |
| `pr-20-multi-engine-routing/` | PR #20 | The same skeleton plus `routing.py` and a routing snapshot: multiple engines, endpoint ACL and owner scoping, with `selftest_routing.py`. Includes the matching `docs/legacy/dell-migration/phase4..6` notes. |
| `pr-16-concurrency-2/` | PR #16 | The concurrency-2 candidate profile (`profiles.py`, `concurrency_selftest.py`). It was a measurement candidate, not a recommendation. |

## What was **not** imported

* Legacy PR #17 (request-body logging) is not present in any form. See
  `docs/legacy/pr-history.md` for the design constraint that came out of it.
* No key material, token, upstream credential or real endpoint host appears in
  these files. Keys in tests are literal non-secret placeholders such as
  `invalid-test-key`.
* No request or response body sample is included.

## Use

Read as design reference. `pr-20` is the closest to what the integrated gateway
needs, but its ACL model must be re-checked against the current portal's
authentication design before any code is lifted.
