# Railway Cost Audit Notes

Use this reference when the main skill needs more detail without loading the full PDF.

## Cost Drivers

Railway spend is primarily RAM, CPU, public network egress, and volume storage. The fastest large reductions usually come from memory first, egress second, CPU/runtime behavior third, and volume last.

Large reductions are plausible only when the architecture has structural waste:

- always-on services that should sleep, run as cron, run as functions, or scale to zero when idle
- service-to-service traffic over public domains
- colocated apps using public database URLs
- full-stack PR environments duplicating production
- static or file traffic served through app processes
- too many workers, replicas, or process clusters
- app/database region mismatch that drives latency and over-scaling

## Audit Checklist

- Pull usage by memory, CPU, egress, and volume.
- List services, environments, replicas, regions, public domains, and volumes.
- Pull 7-day CPU, memory, network, volume, and HTTP metrics.
- Identify low request volume plus persistent memory.
- Identify high egress routes/assets and public internal traffic.
- Verify related services are in the same project/environment.
- Replace internal public hostnames with private-network references.
- Confirm app services use private DB URLs, not public DB URLs.
- Move periodic scripts to cron/jobs when they do not need to stay resident.
- Move large downloads to buckets or presigned URLs.
- Use CDN/cache headers for reusable public responses.
- Split monorepo watch paths.
- Bound worker counts, thread counts, replicas, and DB pool sizes.
- Document rollback and before/after metrics for each change.

## Patterns

Preferred internal references:

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
API_INTERNAL_BASE_URL=http://${{api.RAILWAY_PRIVATE_DOMAIN}}:${{api.PORT}}
```

Avoid for colocated internal traffic:

```text
DATABASE_PUBLIC_URL
RAILWAY_PUBLIC_DOMAIN
```

Cache/file ladder:

1. Cache reusable responses.
2. Use CDN for edge-friendly public content.
3. Use buckets/presigned URLs for file-like downloads.
4. Keep app processes for dynamic user-specific work only.
