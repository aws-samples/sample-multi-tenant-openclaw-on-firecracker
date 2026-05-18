# OpenClaw Local Development Mode (issue #24)

Run the orchestrator Lambda + a stub host-agent **without an AWS account**, so
contributors can iterate on `deploy/lambda/api/handler.py` and friends locally.

## What's mocked

| Component | Mocked by | Endpoint |
|---|---|---|
| DynamoDB / S3 / Lambda / IAM / SSM | LocalStack 3 | `http://localhost:4566` |
| host-agent (per-VM probe + Prom exporter) | Python stub | `http://localhost:8899/health`, `:9090/metrics` |

What's **not** mocked: Firecracker microVMs (those need KVM and a real Linux
host). Use this mode for orchestrator/Lambda iteration, and a real EC2
host for end-to-end testing.

## Quick start

```bash
cd local-dev/
./start.sh
```

Tables (`openclaw-tenants`, `openclaw-hosts`) and the assets S3 bucket are
created automatically. The host-agent stub starts listening on
`localhost:8899` and `:9090`.

Hit it directly:

```bash
curl http://localhost:8899/health
curl http://localhost:9090/metrics
```

Run a one-shot invocation of the API Lambda against LocalStack:

```bash
docker compose --profile full run --rm api-lambda
```

## Tear down

```bash
./stop.sh
```

This `docker compose down -v` removes the LocalStack volume so the next
start is fresh.

## Why a stub host-agent instead of the real one?

The real `host-agent.py` shells out to `pgrep`, reads `/data/firecracker-vms`,
and pings guest IPs — none of which exist on a developer laptop. The stub
serves the same HTTP surface (`/health`, `/metrics`) with synthetic data so
upstream consumers (console, ADOT collector) see realistic shape.

## Files

- `docker-compose.yml` — service definitions
- `.env.example` — template; copy to `.env` and edit
- `start.sh` / `stop.sh` — convenience wrappers
- `host-agent-stub/stub.py` — synthetic host-agent
- `localstack-init/*.sh` — optional scripts run when LocalStack reaches
  the ready state (currently empty; add custom seed data here)
