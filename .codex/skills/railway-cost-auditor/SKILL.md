---
name: railway-cost-auditor
description: Audit and reduce Railway spend without slowing user-facing workflows. Use when working on Railway projects, Railway MCP/CLI metrics, service configuration, infrastructure cost, memory/CPU/egress/volume spend, private networking, serverless sleep, cron/function conversion, preview environments, watch paths, CDN/cache headers, buckets, process fan-out, or repo changes intended to cut Railway cost.
---

# Railway Cost Auditor

## Objective

Reduce Railway spend by removing structural waste before tuning code. Target a 90% reduction only when evidence shows idle compute, public internal egress, duplicated preview stacks, app-served files, or oversized worker/process topology. Do not claim the target was hit without before/after metrics or a run-rate calculation.

Use the bundled PDF reference only when deeper background is needed:
- `references/railway-cost-audit-research.pdf`
- `references/railway-cost-audit-notes.md`

## Operating Rules

1. Use the `use-railway` skill and Railway MCP/CLI with explicit `project`, `environment`, and `service` IDs.
2. Start with evidence: Railway status, 7-day metrics, service config, variable keys with values redacted, domains, deployment state, and repo config.
3. Never print credentials, API secrets, private keys, bearer tokens, database passwords, OTPs, seed phrases, or sealed variable values.
4. Do not delete services, drop databases, remove volumes, or disable production-critical workers without explicit user confirmation.
5. Prefer changes that lower cost and latency together: colocated services, private networking, cache headers, static serving, fewer unnecessary processes, and only-on-when-needed compute.
6. Always document rollback for every live Railway mutation.

## Graph-First Repo Pass

Before broad file reads:

1. Read `graphify-out/GRAPH_REPORT.md`.
2. If available, use `graphify query`, `graphify path`, or `graphify explain` to locate Railway, Docker, process, job, and networking files.
3. Inspect only the smallest relevant set: `railway.toml`, `Dockerfile`, start scripts, job runners, `package.json`, backend launch config, cache/header code, and docs/runbooks.
4. After code/docs/skill/site edits, run `./scripts/graphify.sh update .` or the repo-root fallback from `AGENTS.md`.

## Audit Order

1. Memory: always-on services, worker count, replicas, idle containers, duplicated jobs, preview environments, dev servers in production.
2. Egress: public database URLs, public service-to-service calls, file downloads through app services, uncacheable static responses, telemetry exporters.
3. CPU: SSR that could be SSG/ISR/static, chatty polling, synchronous heavy endpoints, restart loops, oversized process fan-out.
4. Volume: stale persistent data, cache data stored on paid volumes, oversized or unused volumes.

Treat any spend category below 5% as low priority unless it creates operational risk.

## Railway Evidence Commands

Use MCP read tools when available. CLI fallback examples:

```bash
railway project list --json
railway status --project <project-id> --environment production --json
railway environment config --project <project-id> --environment production --json
railway metrics --all --project <project-id> --environment production --since 7d --json
railway metrics --all --project <project-id> --environment production --since 1h --json
railway logs --service <service> --environment production --lines 200 --json
railway logs --service <service> --network --lines 100 --json
railway variable list --project <project-id> --environment production --service <service> --json
```

When reading variables, report keys and safe classifications only: `private-ref`, `public-url-ref`, `public-db-ref`, `boolean`, `set`, or `unset`.

## High-Leverage Fixes

Apply in this order when evidence supports them:

1. Idle compute: convert sparse workers to cron/functions or scale idle noncritical services down. Use serverless sleep only when cold starts will not hurt critical flows and outbound background traffic will not keep the service awake.
2. Private networking: use `${{Postgres.DATABASE_URL}}`, `${{Redis.REDIS_URL}}`, and `http://${{service.RAILWAY_PRIVATE_DOMAIN}}:${{service.PORT}}` for Railway-internal traffic. Avoid `DATABASE_PUBLIC_URL` and public domains from colocated services.
3. Environment multiplication: remove stale non-production environments, enable focused PR environments, and add watch paths so monorepos do not rebuild unrelated services.
4. Public bytes: use CDN/cache headers for reusable responses and buckets or presigned URLs for user files, exports, images, reports, and downloads.
5. Frontends: prefer static output or ISR over SSR where the route does not require per-request server rendering. Use Next.js standalone output for self-hosted runtime.
6. Process model: reduce replicas/workers until load data justifies them. For Python/Gunicorn, test `--preload` where safe. For Node, avoid unnecessary clusters. Bound database pool sizes.
7. Database/cache: keep app and database in the same region, use pooling, and remove polling loops that can become event-driven.

## Fleet-Specific Checks

For these repos, always check:

- `samuel-edge-private / frostv3`: `FROST_RAILWAY_LEAN_JOBS=1`; keep only required proxy exchanges; avoid enabling `FROST_START_DB` unless needed.
- `samuel-edge-private / execution-system`: `RAILWAY_LEAN_PROFILE=true`, `RailwayResources__LeanProfile=true`, Railway provisioning poll defaults, private DB URL, and memory-conserving .NET settings.
- `samuel-edge-private / frost-model-training`: if no training job is expected, confirm whether it should be scaled to zero or converted to run-to-completion.
- Egress proxy services: keep only the required region for exchange access; do not add public domains; verify low network volume.
- `samuel-edge-prod`: keep public frontend/backend hot only when user-facing latency requires it; database traffic must stay private.
- `Savor Everything`: split web/API deploy triggers, move sync/repair/scrape scripts to cron/jobs, and confirm API uses private Postgres.

## Output Contract

Every audit response must include:

- Scope: project, environment, services, and time window.
- Before metrics: memory, CPU, egress, ingress, volume, replicas, sleep, regions, domains.
- Changes made or recommended, with rollback.
- After metrics or expected run-rate, clearly labeled.
- User impact: latency, availability, cold start, data freshness, and operational risk.
- Remaining blockers to the 90% target.

## Rollback Patterns

Use explicit commands and service IDs. Examples:

```bash
railway variable set FROST_RAILWAY_LEAN_JOBS=0 --project <project-id> --environment production --service frostv3
railway variable set SamuelEdgeProvisioning__PollSeconds=5 --project <project-id> --environment production --service execution-system
railway scale -p <project-id> -e production -s <service> us-west2=1
railway redeploy --project <project-id> --environment production --service <service> --yes
```
