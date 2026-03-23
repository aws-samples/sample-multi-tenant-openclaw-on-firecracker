# Contributing Guide

## Before You Start

1. **Sync with main** — Always rebase onto latest `main` before starting work:
   ```bash
   git fetch origin && git rebase origin/main
   ```
2. **Check existing code** — Read the current implementation before writing new code. Do not re-implement features that already exist on `main`.

## Pull Request Rules

- **One concern per PR** — Separate bug fixes, new features, and doc updates into different PRs.
- **Rebase before opening PR** — Your branch must be up-to-date with `main`. Resolve conflicts on your side.
- **Small PRs** — Aim for < 200 lines changed. If larger, explain why in the PR description.
- **Draft PR first for big changes** — Open a draft PR to discuss architecture decisions before writing full implementation.

## Code Conventions

### CDK (deploy/stack.py)
- Use **L2 constructs** (e.g. `alb.add_listener()`), not L1 (`CfnListener`) unless there is a specific reason documented in a code comment.
- **Least-privilege IAM** — Specify exact actions (`elbv2:CreateTargetGroup`, `elbv2:DeleteRule`, ...), never use wildcards (`elbv2:*`).
- **Least-privilege Security Groups** — Use VPC CIDR (`vpc.vpc_cidr_block`) or specific SG references, not `0.0.0.0/0`, for internal traffic.

### Shell Scripts
- Target **Linux** (Ubuntu). Use `sed -i.bak` (not `sed -i` or `sed -i ''`) for cross-platform compatibility:
  ```bash
  sed -i.bak 's/old/new/' file && rm -f file.bak
  ```
- Use `#!/usr/bin/env bash` and `set -euo pipefail`.

### Python (Lambda)
- Keep handlers minimal — extract logic into small named functions.
- Use `os.environ.get()` with sensible defaults for all env vars.

## Commit Messages

Format: `type: short description`

Types: `feat`, `fix`, `docs`, `refactor`, `chore`

Examples:
```
feat: add AgentCore Identity resource
fix: resolve CDK circular dependency in ALB section
docs: update README with AgentCore usage guide
```

## Version Bumps

Do **not** bump `VERSION` or edit `CHANGELOG.md` in your PR. The maintainer assigns the version number at merge time based on the combined changes going into the release. This avoids version conflicts when multiple PRs are in flight.

In your PR description, clearly state whether the change is a **feature**, **fix**, or **breaking change** so the maintainer can determine the appropriate version bump.
