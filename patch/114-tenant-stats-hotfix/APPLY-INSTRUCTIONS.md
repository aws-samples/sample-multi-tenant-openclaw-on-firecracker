# Tenant statistics hotfix

This patch updates a live deployment without running a full CloudFormation
deployment. Open Claude Code in this directory so it loads `CLAUDE.md`, then ask
it to apply the patch.

The tracked files are target-agnostic. `factory/scripts/prepare.sh` first confirms
the live account, region, control-plane API, deployed stage, Lambda function, and
alias. It then writes three target-bound kits under the repository's ignored
`build/` directory. A fresh tool-free Claude process reviews the exact generated
bytes and seals each kit before the runtime allows any write.

The fixed dependency order is:

1. create and verify `openclaw-tenant-stats`;
2. overlay the control-plane Lambda, verify `$LATEST`, publish, and move its
   serving alias;
3. add and deploy `GET /tenants-stats` on the confirmed REST API stage.

The generated set driver performs read-only preflight, plan review, one operator
decision batch, apply, independent live verify, and a second full run proving
idempotency. It stops on the first failure and records the partial state.

No deployment coordinates, credentials, generated kits, or run evidence belong
in Git.
