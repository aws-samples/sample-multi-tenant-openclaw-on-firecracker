# SwarmClaw on Firecracker

This fork keeps the original AWS control plane and Firecracker tenant isolation, but changes the guest runtime from an OpenClaw gateway to a SwarmClaw server.

## Runtime Model

- One tenant still maps to one Firecracker microVM.
- Each tenant VM runs SwarmClaw on the in-guest app port from `config.yml` (`vm.app_port`, default `3456`).
- Host-side tenant routing still allocates unique external ports from `vm.gateway_port_base` (`18789`, `18790`, ...).
- SwarmClaw state lives on the tenant data disk under `/home/agent/.swarmclaw`, so backup, restore, clone, live migration, and disk resize preserve the full tenant workspace.
- The platform still provisions the same AWS primitives: VPC, ASG, ALB, DynamoDB, S3 assets, CloudFront, WAF, Cognito, AMP/Grafana, SNS, and optional Bedrock AgentCore resources.

## Tenant Templates

`config_template` now refers to a SwarmClaw environment template:

```text
s3://<assets-bucket>/templates/swarmclaw/<template-name>/.env.local
```

At launch, the template is appended before the per-tenant generated values:

- `SWARMCLAW_HOME`
- `DATA_DIR`
- `PORT`
- `HOSTNAME`
- `SWARMCLAW_ACCESS_KEY`
- `SWARMCLAW_API_KEY`
- `CREDENTIAL_SECRET`

Use templates for provider and harness defaults. Do not put tenant-shared secrets in the rootfs.

## Build

After deploying the CDK stack, build the SwarmClaw rootfs the same way as before:

```bash
./scripts/build-rootfs-on-ec2.sh v1.0
```

The manifest points hosts to:

- `swarmclaw-rootfs-<version>.ext4.gz`
- `swarmclaw-data-template-<version>.ext4.gz`

Hosts download those images and expose each tenant through `/vm/<tenant-id>`.
