# Grafana provisioning (legacy)

Sanitized Grafana assets from `deploy/dell/grafana/` in the private `llm-lab`
repository.

| Path | Purpose |
|---|---|
| `dashboards/llm-lab-overview.json` | Dashboard definition (node overview, request rate, latency, resource panels). |
| `provisioning/dashboards/llm-lab.yml` | Dashboard provider. The dashboard path is a quoted placeholder string. |
| `provisioning/datasources/prometheus.yml` | Prometheus datasource. The datasource UID was removed so it is assigned at provisioning time. |

## Sanitization

* The datasource UID and the node address were removed or replaced.
* The dashboard JSON was reduced to definitions; no scrape target, hostname or
  credential is present.
* `localhost` is used for loopback.

## Status

Reference only; not wired into any deployment. If this dashboard is adopted,
re-create the datasource with an explicit UID instead of relying on an implicit
one.
