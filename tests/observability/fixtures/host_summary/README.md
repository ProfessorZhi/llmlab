# Host summary contract fixtures (Lane Data #47 → Lane UI #44)

These JSON files are the **hand-off** for issue
[#38](https://github.com/ProfessorZhi/llm-lab-portal/issues/38): the data contract and
boundary conditions for `web/js/infra/host-status.js`.

They are **shared** input. The Python suite
(`tests/observability/test_host_refresh.py`) asserts them against the server-side
validator `backend/observability/host_projection.py`; the UI lane's JS suite should
assert the *same* files against `hostStatusState` / `hostStatusBody`. One fixture, one
expected answer, two consumers — that is the point.

## Do not edit these files in the UI lane

They are owned by the Data lane together with the contract they encode. If a fixture
looks wrong, open it as a contract question — do not silently retune it to match the
implementation. (A fixture edited to agree with the code stops being evidence.)

## File shape

```jsonc
{
  "note": "why this case exists, in one sentence",
  "now": "2026-10-16T12:00:00Z",   // the reference wall clock for this case
  "expect": {
    "accepted": true,               // is the *envelope* trusted?
    "any_live": true,               // may anything in it be shown as a real reading?
    "live_keys": ["gpu_utilization_ratio", "..."],  // exactly the keys that may be LIVE
    "envelope_reason": null,        // why the whole body was rejected (null when accepted)
    "metric_reasons": [],           // distinct reasons on the NON-live metrics, sorted
    "display_state": "live"         // what the panel badge must say: live | unknown
  },
  "payload": { /* the /api/infra/summary body, or a failure body */ }
}
```

`expect.display_state` is `live` **iff** `accepted && any_live`. There is no third
state, and there is no fallback to the demo fixture.

## The cases

| File | What it pins down |
| --- | --- |
| `01-live-fresh.json` | The only shape that may show LIVE. |
| `02-partial-unknown.json` | One missing metric stays UNKNOWN while the rest are LIVE. |
| `03-zero-measured.json` | A measured `0` is LIVE and stays `0` — never confused with "no sample". |
| `04-absent-sample-at.json` | `state: "known"` with no `sample_at` ⇒ **not LIVE**. |
| `05-future-sample-at.json` | A future timestamp is clock skew, not freshness ⇒ **not LIVE**. |
| `06-synthetic-true.json` | `synthetic: true` ⇒ never LIVE, whatever else looks right. |
| `07-wrong-source.json` | A source outside the whitelist ⇒ never LIVE. |
| `08-stale-sample-at.json` | Samples older than 90 s ⇒ UNKNOWN, not "old but fine". |
| `09-offline.json` | The projection's offline shape: complete, all-unknown, not an error. |
| `10-not-found.json` | What a 404 body actually looks like ⇒ fail closed to UNKNOWN. |

## Required client behaviour (issue #38)

The reference state machine is `backend/observability/host_refresh.py`; it is a
porting target, not a second implementation. Port these rules, and port the tests:

1. **One request in flight at a time.** A slow response must not be overlapped by a
   second request; two in-flight responses race for the same UI state.
2. **Hidden tab stops polling.** While `document.hidden`, fire no new request and do
   not spin a timer.
3. **Visible tab refreshes at once.** On resume, if the interval has elapsed since the
   last request, fetch immediately rather than waiting out another full period.
4. **Age is re-checked on every paint, not only on every response.** After
   `max_sample_age_s` (90 s) the panel must drop out of LIVE *even if no new response
   arrived* — a tab suspended overnight must not keep showing last night's number.
5. **Failure clears the stale value.** Timeout, disconnect, 401/403/404 and any
   contract mismatch must clear the number and paint UNKNOWN. Keep the reason distinct:
   `not-yet-fetched`, `request-failed`, `contract-violation`, `expired`,
   `clock-rollback`.
6. **Late responses never overwrite newer ones.** Sequence every request; apply only
   the response belonging to the newest request.
7. **One snapshot per paint.** GPU utilisation, memory, temperature, power and the
   service health row must all come from the *same* response — no mixing of two
   fetches, no second request just for the footer.
8. **Only same-origin.** One fixed same-origin GET (`/api/infra/summary`); never reach
   `9090`/`8200`/`9000`/`8080` from the browser, and never print a user or a key.

## Regenerating

Do not hand-edit. The fixtures are produced from a single reference clock so the
timestamps stay mutually consistent; the generator lives outside the repository and the
files are committed as generated artefacts.
