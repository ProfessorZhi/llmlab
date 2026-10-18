"""Named concurrency profiles, so a candidate limit is a word and not a typo.

WHY THIS FILE EXISTS, AND WHY IT IS NOT A NEW DEFAULT.
------------------------------------------------------
`limits.py` states the rule that governs every number here:

    the *global* limit ... is the number that must match `parallel`.

The deployment runs Strata with `parallel 1` (`config/llm-lab.example.toml`:
`parallel 1`; `reports/deployment/Strata_Qwen3.8_Deployment_Report.md` §10
records `parallel=2` measured slower than serial and `parallel: 1` landed), so
the gateway's global limit is 1 and has been since the first release.

The question "raise the concurrency a little" therefore arrives as a *change to
an agreed pairing of two numbers*, not as a tuning tweak.  The two numbers live
in two different systems, on two different hosts, one of which is not writable
from here -- so the way to make that change reviewable is to name the pairing
and keep the current one as the default:

  * a **profile** is one named set of the five admission knobs, plus the
    `parallel` value the engine must be running for the profile to be honest;
  * **`production` is the default and is exactly the numbers already deployed**
    -- 1 / 1 / 8 / 120 -- so importing this module cannot widen anything;
  * **`concurrency-2-candidate`** is the candidate the brief asks for:
    `global 2`, and `per_user 1` *unchanged*, because whether one caller may
    hold both slots is a separate decision that has not been taken.

WHAT A PROFILE MUST NOT BE ABLE TO DO.  A profile that raised `global_limit`
above the engine's `parallel` would not add capacity -- it would move the queue
out of this process, where it is bounded by `queue_limit`, visible in
`status.json` and reported as `queue_ms`, and into the engine, where none of
those three things are true.  The admission gate would report an idle queue
while callers waited upstream, and the 429 that `queue_limit` exists to produce
would stop being produced.  So `global_limit == engine_parallel_required` is
enforced below as an invariant of the table rather than left as a comment: a
profile that breaks it is a startup error, on the same principle as
`parse_aliases` refusing a malformed alias instead of skipping it.

The second invariant is that **no profile may turn the limits off**.  The
release criterion for the candidate is that rate limiting is *retained* as the
thing standing between a second concurrent generation and a 49 GiB card whose
measured peak was already 40,714 MiB.  A profile with `queue_limit 0` or no
per-user cap would be a profile that removes the safety rather than raising the
throughput, so `queue_limit >= 1`, `per_user_limit >= 1` and `wait_timeout > 0`
are checked too.
"""

from __future__ import annotations

# The profile used when the operator names none.  It is the deployed
# configuration, value for value; see `config/dell-site.example.toml`
# `[services.api-gateway]`.
DEFAULT_PROFILE = "production"

# Every profile states the same five admission knobs (argparse destinations, so
# a profile supplies them to `parse_args` as defaults) plus the engine
# `parallel` it requires.  Keeping the smoke-test keys beside the values is what
# makes the pairing reviewable in one screen.
PROFILES: dict[str, dict] = {
    "production": {
        "global_limit": 1,
        "per_user_limit": 1,
        "queue_limit": 8,
        "wait_timeout": 120.0,
        "retry_after": 5.0,
        "engine_parallel_required": 1,
        "note": "What is deployed. One engine slot, so two callers queue in "
                "the open instead of both going slow against the model.",
    },
    "concurrency-2-candidate": {
        "global_limit": 2,
        "per_user_limit": 1,
        "queue_limit": 8,
        "wait_timeout": 120.0,
        "retry_after": 5.0,
        "engine_parallel_required": 2,
        # NOT a production change and not a claim: this profile is only honest
        # once Strata itself runs `parallel 2`.  Until then it is the *gateway*
        # half of a two-part change, and applying it alone moves the queue to
        # the engine (see the module docstring).  The procedure, the rollback
        # and the criteria are in `docs/dell-migration/phase5-api-gateway.md`
        # §11.
        "note": "Candidate: two engine slots. Requires Strata `parallel 2`; "
                "per-user stays 1, so one caller still cannot hold both.",
    },
}

# The knobs a profile supplies to the argument parser.  A profile carrying a key
# outside this set is a typo that would otherwise be silently ignored (the value
# would never reach the gate), so it is a startup error instead.
SETTINGS_KEYS = ("global_limit", "per_user_limit", "queue_limit",
                 "wait_timeout", "retry_after")


def _validate() -> None:
    """Refuse, at import, a profile that would weaken or misstate the gate.

    Import-time rather than call-time on purpose: these are properties of the
    table as written, they cost nothing to check once, and a bad entry should
    fail the process before it binds a port.
    """
    for name, prof in PROFILES.items():
        unknown = sorted(set(prof) - set(SETTINGS_KEYS)
                         - {"engine_parallel_required", "note"})
        if unknown:
            raise SystemExit("profile %r carries keys the gateway does not "
                             "read: %s" % (name, ", ".join(unknown)))
        if prof["global_limit"] != prof["engine_parallel_required"]:
            raise SystemExit(
                "profile %r asks for global_limit=%d but declares the engine "
                "runs parallel=%d; the global limit is the number that must "
                "match parallel (limits.py), so this profile would move the "
                "queue into the engine where nothing bounds it"
                % (name, prof["global_limit"], prof["engine_parallel_required"]))
        if prof["per_user_limit"] < 1:
            raise SystemExit("profile %r caps a user at %d requests; a profile "
                             "may not remove the per-user limit"
                             % (name, prof["per_user_limit"]))
        if prof["queue_limit"] < 1:
            raise SystemExit("profile %r sets queue_limit=%d, which refuses "
                             "every waiting caller immediately; the candidate "
                             "keeps a queue, it does not remove the refusal "
                             "path" % (name, prof["queue_limit"]))
        if prof["wait_timeout"] <= 0:
            raise SystemExit("profile %r sets wait_timeout=%r; a zero or "
                             "negative wait is a refusal dressed as a queue"
                             % (name, prof["wait_timeout"]))


_validate()


def names() -> list[str]:
    """Every profile, sorted, with the default first -- what `--help` prints."""
    return [DEFAULT_PROFILE] + sorted(n for n in PROFILES if n != DEFAULT_PROFILE)


def settings(name: str) -> dict:
    """The five admission knobs for `name`, ready for `ArgumentParser.set_defaults`.

    Returns a fresh dict: the caller gets argparse's defaults, and the table
    above stays read-only.
    """
    prof = PROFILES.get(name)
    if prof is None:
        raise SystemExit("no such profile %r; known profiles are: %s"
                         % (name, ", ".join(names())))
    return {k: prof[k] for k in SETTINGS_KEYS}


def engine_parallel_required(name: str) -> int:
    """The Strata `parallel` this profile is only honest with.

    Read by the acceptance checks and by the operator procedure, so the pairing
    is stated in exactly one place.
    """
    prof = PROFILES.get(name)
    if prof is None:
        raise SystemExit("no such profile %r; known profiles are: %s"
                         % (name, ", ".join(names())))
    return int(prof["engine_parallel_required"])


def note(name: str) -> str:
    """The one-line reason, for the startup banner and the status document."""
    return (PROFILES.get(name) or {}).get("note", "")
