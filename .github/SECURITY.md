# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, please notify AWS/Amazon Security via the [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Do **not** create a public GitHub issue.

Please include enough detail to reproduce: affected component, a minimal reproduction or sequence of steps, and the impact you observed.

## Supported Versions

This is an AWS sample. Fixes land on the latest release only — upgrade to the newest tag before reporting, and see the [Upgrade Guide](../README.md#%EF%B8%8F-upgrade-guide) for the any-version-to-latest path.

| Version | Supported |
|---------|-----------|
| Latest `1.5.x` | ✅ |
| Older | ❌ — upgrade to latest |

## Scope

This sample provisions real AWS infrastructure (Firecracker hosts, Lambda, API Gateway, Cognito, DynamoDB, S3). Security-relevant design — tenant isolation, network `FORWARD DROP` including the host IMDS, RBAC with RS256 id_token verification, least-privilege IAM/SG — is documented in the [README security sections](../README.md). Report anything that weakens these guarantees, plus the usual classes (authn/authz bypass, injection, secret exposure, privilege escalation across tenants).
