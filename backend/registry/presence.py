"""Freshness of owner-Agent observations; stale never means proven stopped."""
from datetime import datetime, timezone
from .agent_reports import STALE_AFTER_SECONDS


def service_state(engine, *, now=None):
    if engine.state == 'offline':
        return 'offline'
    if (engine.launch or {}).get('agent_id'):
        last = engine.last_seen_at
        if last is None or ((now or datetime.now(timezone.utc)) - last).total_seconds() > STALE_AFTER_SECONDS:
            return 'stale'
    return engine.state
