# Contributing Guide

Thank you for your interest in contributing to this project! We welcome bug reports, feature requests, and pull requests.

## How to Contribute

### Reporting Bugs / Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open and recently closed issues to make sure somebody else hasn't already reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

- A reproducible test case or series of steps
- The version of the code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

### Contributing via Pull Requests

1. Fork the repository and create your branch from `main`.
2. If you've added code that should be tested, add tests.
3. Ensure the test suite passes.
4. Make sure your code follows the existing style conventions.
5. Submit a pull request.

### Pull Request Rules

- **One concern per PR** — Separate bug fixes, new features, and doc updates into different PRs.
- **Rebase before opening PR** — Your branch must be up-to-date with `main`. Resolve conflicts on your side.
- **Small PRs** — Aim for < 200 lines changed. If larger, explain why in the PR description.

### Code Conventions

#### CDK (deploy/stack.py)
- Use **L2 constructs** (e.g. `alb.add_listener()`), not L1 (`CfnListener`) unless there is a specific reason documented in a code comment.
- **Least-privilege IAM** — Specify exact actions, never use wildcards (`elbv2:*`).
- **Least-privilege Security Groups** — Use VPC CIDR or specific SG references, not `0.0.0.0/0`, for internal traffic.

#### Shell Scripts
- Target **Linux** (Ubuntu). Use `#!/usr/bin/env bash` and `set -euo pipefail`.

#### Python (Lambda)
- Keep handlers minimal — extract logic into small named functions.
- Use `os.environ.get()` with sensible defaults for all env vars.

### Commit Messages

Format: `type: short description`

Types: `feat`, `fix`, `docs`, `refactor`, `chore`

## Finding Contributions to Work On

Looking at the existing issues is a great way to find something to contribute on.

## Security Issue Notifications

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
