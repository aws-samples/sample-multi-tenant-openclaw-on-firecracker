# Hetzner Ubuntu Install

This backend target runs XoomAI-managed SwarmClaw tenants on one Ubuntu host without managed cloud services.

## Host Requirement

Use an Ubuntu host where you control system packages, firewall rules, nginx, systemd services, and tenant workload directories.

Recommended baseline:

- Ubuntu 22.04 or 24.04
- 4+ CPU cores
- 16 GB+ RAM
- 100 GB+ local SSD
- A dedicated public IP
- SSH access with sudo

## What The Hetzner Host Provides

| Role | Hetzner version |
|---|---|
| Control API | `xoomai-swarmclaw-api.service` |
| Tenant table | `/data/xoomai-swarmclaw/state/tenants.json` |
| Tenant templates | `/data/xoomai-swarmclaw/templates` |
| Tenant backups | `/data/xoomai-swarmclaw/backups` |
| Command runner | direct local subprocess calls |
| Public routing | host nginx |
| API auth | local API key in `/etc/xoomai-swarmclaw/api-key` |

## Install

On the Ubuntu Hetzner host:

```bash
sudo scripts/hetzner/install.sh
```

The install flow should:

1. Install system packages.
2. Create `/opt/xoomai-swarmclaw`.
3. Create `/data/xoomai-swarmclaw`.
4. Create `/etc/xoomai-swarmclaw/api-key`.
5. Install the local API as `xoomai-swarmclaw-api.service`.
6. Configure nginx tenant routes.

## Create A Tenant

```bash
API_KEY="$(sudo cat /etc/xoomai-swarmclaw/api-key)"

curl -s -X POST http://127.0.0.1:8080/tenants \
  -H "x-api-key: ${API_KEY}" \
  -H "content-type: application/json" \
  -d '{"name":"demo","cpu":2,"memory_mb":4096}' | jq .
```

The response includes `access_key`, which is the generated SwarmClaw access key for that tenant.

## Operations

```bash
curl -s http://127.0.0.1:8080/tenants -H "x-api-key: ${API_KEY}" | jq .
curl -s -X POST http://127.0.0.1:8080/tenants/<id>/stop -H "x-api-key: ${API_KEY}" | jq .
curl -s -X POST http://127.0.0.1:8080/tenants/<id>/start -H "x-api-key: ${API_KEY}" | jq .
curl -s -X POST http://127.0.0.1:8080/tenants/<id>/backup -H "x-api-key: ${API_KEY}" | jq .
curl -s -X POST http://127.0.0.1:8080/tenants/<id>/clone \
  -H "x-api-key: ${API_KEY}" \
  -H "content-type: application/json" \
  -d '{"name":"demo-clone"}' | jq .
curl -s -X DELETE http://127.0.0.1:8080/tenants/<id> -H "x-api-key: ${API_KEY}" | jq .
```

## Templates

Tenant `config_template` names map to local SwarmClaw env templates:

```text
/data/xoomai-swarmclaw/templates/<template-name>/.env.local
```

The launcher appends generated per-tenant values after the template, including:

- `SWARMCLAW_HOME`
- `DATA_DIR`
- `PORT`
- `HOSTNAME`
- `SWARMCLAW_ACCESS_KEY`
- `SWARMCLAW_API_KEY`
- `CREDENTIAL_SECRET`

## Notes

This is a single-host backend. Backups are local files; copy `/data/xoomai-swarmclaw/backups` off-host if you want disaster recovery.
