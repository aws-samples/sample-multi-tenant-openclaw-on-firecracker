# Hetzner Ubuntu Install

This backend runs the SwarmClaw Firecracker pool on one Ubuntu host without AWS.

## Host Requirement

Firecracker requires `/dev/kvm`. Hetzner Cloud VPS instances do not expose nested virtualization, so this target is for Hetzner dedicated/root/bare-metal servers, or any Ubuntu host where:

```bash
test -e /dev/kvm && echo ok
```

The installer fails fast if `/dev/kvm` is missing.

## What Replaces AWS

| AWS version | Hetzner version |
|---|---|
| Lambda API | `swarmclaw-firecracker-api.service` |
| DynamoDB tenant table | `/data/swarmclaw-firecracker/state/tenants.json` |
| S3 rootfs assets | `/data/swarmclaw-firecracker/rootfs` and `/data/firecracker-assets` |
| S3 backups | `/data/swarmclaw-firecracker/backups` |
| SSM commands | direct local subprocess calls |
| ALB / CloudFront | host nginx on port 80 |
| Cognito / API Gateway key | local API key in `/etc/swarmclaw-firecracker/api-key` |

## Install

On the Ubuntu Hetzner host:

```bash
sudo scripts/hetzner/install.sh \
  --kernel-path /path/to/firecracker-compatible-vmlinux \
  --build-rootfs \
  --version v1.0
```

If you want to install the control plane first and build the rootfs later:

```bash
sudo scripts/hetzner/install.sh --kernel-path /path/to/firecracker-compatible-vmlinux
sudo HETZNER_LOCAL=1 \
  LOCAL_OUTPUT_DIR=/data/swarmclaw-firecracker/rootfs \
  LOCAL_INSTALL_DIR=/data/firecracker-assets \
  ./build-rootfs.sh v1.0
sudo systemctl restart swarmclaw-firecracker-api
```

The installer intentionally does not download a kernel from AWS. Provide a local `vmlinux` with `--kernel-path`, a non-AWS URL with `--kernel-url`, or place it at `/data/firecracker-assets/vmlinux` before starting tenants.

## Create A Tenant

```bash
API_KEY="$(sudo cat /etc/swarmclaw-firecracker/api-key)"

curl -s -X POST http://127.0.0.1:8080/tenants \
  -H "x-api-key: ${API_KEY}" \
  -H "content-type: application/json" \
  -d '{"name":"demo","vcpu":2,"mem_mb":4096}' | jq .
```

Tenant dashboards are served by nginx:

```text
http://<host>/vm/<tenant-id>/
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
/data/swarmclaw-firecracker/templates/<template-name>/.env.local
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

This is a single-host backend. It intentionally does not include AWS-style multi-AZ failover, Auto Scaling Groups, CloudFront, Cognito, managed Prometheus/Grafana, WAF, or Bedrock AgentCore provisioning. Backups are local files; copy `/data/swarmclaw-firecracker/backups` off-host if you want disaster recovery.
