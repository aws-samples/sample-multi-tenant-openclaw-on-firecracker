# SwarmClaw Firecracker Pool for Hetzner

Run isolated, multi-tenant SwarmClaw instances on a single Ubuntu host using Firecracker microVMs.

This fork is aimed at Hetzner dedicated/root/bare-metal servers. It does not depend on a managed cloud control plane: tenant state, rootfs assets, backups, lifecycle operations, and routing all live on the host.

## What This Is

- One tenant maps to one Firecracker microVM.
- Each tenant runs its own SwarmClaw server.
- One tenant can host many SwarmClaw agents across many providers and harnesses.
- Tenant disks live under `/data/firecracker-vms`.
- Tenant dashboards are exposed through host nginx at `/vm/<tenant-id>/`.
- The local control plane is a Python systemd service with JSON state.

## Host Requirement

Firecracker requires KVM. Your host must expose:

```bash
test -e /dev/kvm && echo ok
```

Hetzner Cloud VPS instances do not expose nested virtualization. Use a Hetzner dedicated/root/bare-metal server, or another Ubuntu host with `/dev/kvm`.

## Architecture

```text
Operator
  |
  |  x-api-key
  v
Local API :8080
  |
  | direct subprocess calls
  v
Firecracker host scripts
  |
  +-- /data/firecracker-vms/<tenant-id>/
  |     +-- data.ext4
  |     +-- overlay.ext4
  |     +-- access-key
  |
  +-- tap-vmN -> tenant guest 172.16.N.2
  |
  v
Tenant microVM
  |
  v
SwarmClaw :3456

nginx :80
  |
  +-- /vm/<tenant-id>/ -> http://172.16.N.2:3456/
```

## What Replaces The Managed Cloud Pieces

| Previous role | This fork |
|---|---|
| Remote API functions | `swarmclaw-firecracker-api.service` |
| Managed database | `/data/swarmclaw-firecracker/state/tenants.json` |
| Object storage for assets | `/data/swarmclaw-firecracker/rootfs` and `/data/firecracker-assets` |
| Object storage for backups | `/data/swarmclaw-firecracker/backups` |
| Remote command runner | Direct local subprocess calls |
| Load balancer / CDN | Host nginx |
| Managed auth | Local API key at `/etc/swarmclaw-firecracker/api-key` |

## Quick Start

On the Ubuntu host:

```bash
git clone https://github.com/XoomCloud/multi-tenant-openclaw-on-firecracker.git
cd multi-tenant-openclaw-on-firecracker
git checkout swarmclaw-hetzner-backend
```

Install the local control plane and build the SwarmClaw rootfs:

```bash
sudo scripts/hetzner/install.sh \
  --kernel-path /path/to/firecracker-compatible-vmlinux \
  --build-rootfs \
  --version v1.0
```

If you already placed a compatible kernel at `/data/firecracker-assets/vmlinux`, you can use:

```bash
sudo scripts/hetzner/install.sh --skip-kernel --build-rootfs --version v1.0
```

The installer intentionally does not download a kernel from a cloud-provider bucket. Provide a local kernel with `--kernel-path`, a non-provider URL with `--kernel-url`, or pre-place `/data/firecracker-assets/vmlinux`.

## Create A Tenant

```bash
API_KEY="$(sudo cat /etc/swarmclaw-firecracker/api-key)"

curl -s -X POST http://127.0.0.1:8080/tenants \
  -H "x-api-key: ${API_KEY}" \
  -H "content-type: application/json" \
  -d '{"name":"demo","vcpu":2,"mem_mb":4096}' | jq .
```

The response includes:

- `id`: tenant id
- `dashboard_url`: nginx route for the tenant
- `access_key`: generated SwarmClaw API/access key for that tenant

Open:

```text
http://<host>/vm/<tenant-id>/
```

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

Resize the tenant data disk:

```bash
curl -s -X POST http://127.0.0.1:8080/tenants/<id>/resize-disk \
  -H "x-api-key: ${API_KEY}" \
  -H "content-type: application/json" \
  -d '{"data_disk_mb":16384}' | jq .
```

Delete a tenant:

```bash
curl -s -X DELETE http://127.0.0.1:8080/tenants/<id> \
  -H "x-api-key: ${API_KEY}" | jq .
```

Preserve the tenant disk directory:

```bash
curl -s -X DELETE "http://127.0.0.1:8080/tenants/<id>?keep_data=true" \
  -H "x-api-key: ${API_KEY}" | jq .
```

## SwarmClaw Templates

Tenant `config_template` names map to local environment templates:

```text
/data/swarmclaw-firecracker/templates/<template-name>/.env.local
```

Example:

```bash
sudo mkdir -p /data/swarmclaw-firecracker/templates/openrouter
sudo tee /data/swarmclaw-firecracker/templates/openrouter/.env.local >/dev/null <<'EOF'
OPENROUTER_API_KEY=replace-me
EOF
```

Create a tenant with that template:

```bash
curl -s -X POST http://127.0.0.1:8080/tenants \
  -H "x-api-key: ${API_KEY}" \
  -H "content-type: application/json" \
  -d '{"name":"research","config_template":"openrouter","vcpu":2,"mem_mb":4096}' | jq .
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
| `/etc/swarmclaw-firecracker/api-key` | Local API key |
| `/etc/swarmclaw-firecracker/env` | systemd environment for the local API |
| `/data/firecracker-assets` | Active kernel/rootfs/data template |
| `/data/firecracker-vms` | Tenant VM disks and metadata |
| `/data/swarmclaw-firecracker/state/tenants.json` | Local tenant database |
| `/data/swarmclaw-firecracker/backups` | Local compressed tenant backups |
| `/data/swarmclaw-firecracker/templates` | SwarmClaw env templates |
| `/opt/swarmclaw-firecracker` | Local API and host scripts |

## Services

```bash
sudo systemctl status swarmclaw-firecracker-api
sudo journalctl -u swarmclaw-firecracker-api -f
sudo systemctl restart swarmclaw-firecracker-api
sudo systemctl restart nginx
```

## Build Rootfs Later

```bash
sudo HETZNER_LOCAL=1 \
  LOCAL_OUTPUT_DIR=/data/swarmclaw-firecracker/rootfs \
  LOCAL_INSTALL_DIR=/data/firecracker-assets \
  ./build-rootfs.sh v1.0

sudo systemctl restart swarmclaw-firecracker-api
```

## Security Notes

- Put the local API behind a firewall or private network.
- Rotate `/etc/swarmclaw-firecracker/api-key` if it leaks.
- Do not expose tenant dashboards publicly unless you understand the SwarmClaw access model.
- Backups are local files. Copy `/data/swarmclaw-firecracker/backups` off-host if you need disaster recovery.
- This is a single-host backend. There is no multi-host failover or autoscaling in this fork.

## More Docs

- [Hetzner Ubuntu install guide](docs/HETZNER-UBUNTU.md)
- [SwarmClaw Firecracker runtime notes](docs/SWARMCLAW-FIRECRACKER.md)

## License

MIT-0. See [LICENSE](LICENSE).
