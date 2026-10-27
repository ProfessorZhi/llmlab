"""Adopt authenticated local listeners as private, owner-managed services."""
from __future__ import annotations

from .observed_services import attach_observed
from .models import ENGINE_RUNTIME_ATTACHED, ENGINE_RUNTIME_OFFLINE


def adopt_reported_services(store, *, owner, report, now):
    seen = set()
    created = []
    for item in report.engines:
        if not item.get('registrable') or not (item.get('models') or item.get('framework')):
            continue
        port = item['port']
        # A manual registration already covering this listener keeps its identity.
        matches = [e for e in store.list_engines() if (e.target or {}).get('port') == port]
        if any(e.owner_principal != owner for e in matches):
            continue
        if matches:
            engine = matches[0]
        else:
            engine = attach_observed(store, uid=report.uid, principal=owner, port=port,
                                     framework=item.get('framework'), model_ids=tuple(item.get('models') or ()),
                                     visibility='private', now=now, adopt_model_refs=False)
            created.append(engine.id)
        launch = {**(engine.launch or {}), 'framework': item.get('framework'),
                  'model_ids': list(item.get('models') or ()), 'visibility': 'private',
                  'agent_id': report.agent_id, 'agent_host_id': report.host_id}
        store.update_engine_runtime(engine.id, state=ENGINE_RUNTIME_ATTACHED, last_seen_at=now, launch=launch)
        store.update_engine_status(engine.id, status='verified' if item.get('models') else 'registered',
                                   health={'state': 'healthy' if item.get('models') else 'unknown',
                                           'source': 'owner_agent', 'observed_at': now.isoformat(),
                                           'verified': bool(item.get('models'))},
                                   ownership={'state': 'uid_verified', 'verified': True,
                                              'method': 'authenticated_agent_listener_uid',
                                              'uid': report.uid, 'linux_user': report.linux_user},
                                   last_seen_at=now, updated_at=now)
        seen.add(engine.id)
    # A failed/capped scan never proves that a listener has stopped.
    if report.full and not report.rejected.get('engines') and not any('engine' in p for p in report.problems):
        for engine in store.list_engines():
            launch = engine.launch or {}
            if (engine.owner_principal == owner and engine.id not in seen
                    and launch.get('agent_id') == report.agent_id
                    and launch.get('agent_host_id') == report.host_id):
                store.update_engine_runtime(engine.id, state=ENGINE_RUNTIME_OFFLINE, last_seen_at=engine.last_seen_at)
    return created
