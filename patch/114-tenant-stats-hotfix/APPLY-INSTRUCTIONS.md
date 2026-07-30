# Tenant statistics hotfix

Use `CLAUDE.md` as the executor contract. Before generation, the operator must
confirm the real explicit-resource REST API ID, stage, client URL, and
authentication headers file. Customer `api.mode` does not select the API, and
`ANY /{proxy+}` is never a valid target.

Generate with:

```bash
bash factory/scripts/prepare.sh <region> <customer-config.yml>
```

The output contains three independently reviewed kits. Apply them only through
the packaged `runtime/scripts/patch-set.sh`, in this order:

```text
114-tenant-stats-table -> 114-api-lambda -> 114-tenants-stats-route
```

Completion means all three verify, the authenticated live
`GET /tenants-stats` returns HTTP 200 with a `business` object, and the second
full driver run reports `SKIP` with zero writes.
