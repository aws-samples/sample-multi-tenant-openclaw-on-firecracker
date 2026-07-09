# XoomAI SwarmClaw Tenant Host for Hetzner

Run multi-tenant SwarmClaw instances on an Ubuntu Hetzner host without managed cloud services.

This fork is for a XoomAI Enterprise OS deployment where one tenant can run many agents across multiple harnesses, departments, MCP servers, and external connections. The target runtime is a local Ubuntu control plane with tenant-scoped workspaces, nginx routing, local state, and direct lifecycle automation.

## What This Is

- One tenant maps to one isolated tenant workspace.
- Each tenant can run its own SwarmClaw instance.
- One tenant can host many agents across many harnesses.
- Tenant state lives on the host under `/data/xoomai-swarmclaw`.
- Tenant apps are exposed through nginx tenant routes.
- The local control plane is a Python systemd service with JSON state.

## Target Architecture

```text
Operator / XoomAI Enterprise OS
  |
  |  local API key
  v
Local Control API :8080
  |
  | direct lifecycle automation
  v
Tenant runtime manager
  |
  +-- /data/xoomai-swarmclaw/tenants/<tenant-id>/
  |     +-- workspace
  |     +-- env
  |     +-- state
  |
  v
SwarmClaw tenant runtime :3456

nginx :80/:443
  |
  +-- tenant route -> SwarmClaw tenant runtime
```

## Host Requirements

- Ubuntu 22.04 or 24.04
- Root or sudo access
- nginx
- Python 3.11+
- Node.js 20+
- A process/container runtime for tenant workloads
- Firewall rules that expose only the intended public ports

## Quick Start

On the Ubuntu host:

```bash
git clone <your-fork-url>
cd <repo-dir>
git checkout swarmclaw-hetzner-backend
```

Install the local control plane:

```bash
sudo scripts/hetzner/install.sh
```

The installer should provision:

- `/opt/xoomai-swarmclaw` for control-plane code
- `/etc/xoomai-swarmclaw/api-key` for local API auth
- `/data/xoomai-swarmclaw` for tenant state, templates, and backups
- `xoomai-swarmclaw-api.service` for the local API
- nginx tenant routing

## Create A Tenant

```bash
API_KEY="$(sudo cat /etc/xoomai-swarmclaw/api-key)"

curl -s -X POST http://127.0.0.1:8080/tenants \
  -H "x-api-key: ${API_KEY}" \
  -H "content-type: application/json" \
  -d '{"name":"demo","cpu":2,"memory_mb":4096}' | jq .
```

The response should include:

- `id`: tenant id
- `dashboard_url`: nginx route for the tenant
- `access_key`: generated SwarmClaw access key for that tenant

## Tenant Operations

List tenants:

```bash
curl -s http://127.0.0.1:8080/tenants \
  -H "x-api-key: ${API_KEY}" | jq .
```

Stop, start, or restart:

```bash
curl -s -X POST http://127.0.0.1:8080/tenants/<id>/stop \
  -H "x-api-key: ${API_KEY}" | jq .

curl -s -X POST http://127.0.0.1:8080/tenants/<id>/start \
  -H "x-api-key: ${API_KEY}" | jq .

curl -s -X POST http://127.0.0.1:8080/tenants/<id>/restart \
  -H "x-api-key: ${API_KEY}" | jq .
```

Back up a tenant:

```bash
curl -s -X POST http://127.0.0.1:8080/tenants/<id>/backup \
  -H "x-api-key: ${API_KEY}" | jq .
```

Clone a tenant:

```bash
curl -s -X POST http://127.0.0.1:8080/tenants/<id>/clone \
  -H "x-api-key: ${API_KEY}" \
  -H "content-type: application/json" \
  -d '{"name":"demo-clone"}' | jq .
```

Delete a tenant:

```bash
curl -s -X DELETE http://127.0.0.1:8080/tenants/<id> \
  -H "x-api-key: ${API_KEY}" | jq .
```

## SwarmClaw Templates

Tenant `config_template` names map to local environment templates:

```text
/data/xoomai-swarmclaw/templates/<template-name>/.env.local
```

Example:

```bash
sudo mkdir -p /data/xoomai-swarmclaw/templates/openrouter
sudo tee /data/xoomai-swarmclaw/templates/openrouter/.env.local >/dev/null <<'EOF'
OPENROUTER_API_KEY=replace-me
EOF
```

Create a tenant with that template:

```bash
curl -s -X POST http://127.0.0.1:8080/tenants \
  -H "x-api-key: ${API_KEY}" \
  -H "content-type: application/json" \
  -d '{"name":"research","config_template":"openrouter","cpu":2,"memory_mb":4096}' | jq .
```

Generated tenant values are appended after the template:

- `SWARMCLAW_HOME`
- `DATA_DIR`
- `PORT`
- `HOSTNAME`
- `SWARMCLAW_ACCESS_KEY`
- `SWARMCLAW_API_KEY`
- `CREDENTIAL_SECRET`

## Local Files

| Path | Purpose |
|---|---|
| `/etc/xoomai-swarmclaw/api-key` | Local API key |
| `/etc/xoomai-swarmclaw/env` | systemd environment for the local API |
| `/data/xoomai-swarmclaw/state/tenants.json` | Local tenant database |
| `/data/xoomai-swarmclaw/backups` | Local tenant backups |
| `/data/xoomai-swarmclaw/templates` | SwarmClaw env templates |
| `/opt/xoomai-swarmclaw` | Local API and host scripts |

## Services

```bash
sudo systemctl status xoomai-swarmclaw-api
sudo journalctl -u xoomai-swarmclaw-api -f
sudo systemctl restart xoomai-swarmclaw-api
sudo systemctl restart nginx
```

## Security Notes

- Put the local API behind a firewall or private network.
- Rotate `/etc/xoomai-swarmclaw/api-key` if it leaks.
- Do not expose tenant dashboards publicly unless you understand the SwarmClaw access model.
- Backups are local files. Copy `/data/xoomai-swarmclaw/backups` off-host if you need disaster recovery.
- This is a single-host backend. There is no multi-host failover or autoscaling in this fork.

## More Docs

- [Hetzner Ubuntu install guide](docs/HETZNER-UBUNTU.md)

## License

MIT-0. See [LICENSE](LICENSE).
