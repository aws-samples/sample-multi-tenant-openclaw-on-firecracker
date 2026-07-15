# Reference

This section provides the data collection notes and contributor information for this solution.

## Data collection

The performance, capacity, isolation, and interception numbers in this implementation guide all come from first-hand measurement or are explicitly marked as estimated / to be verified:

- Numbers marked "measured" come from bare-metal measurement, lifecycle measurement, or unit tests, including microVM startup 1.74s, launch to gateway availability 6.48s, blank VM resident memory about 609MB, whole-machine average 669MB, single host fully healthy capacity 187 nodes, jailbreak interception 14/14, malicious-skill interception 41/41, access-control unit tests 8/8, runtime audit overhead about 11.7MB, and file integrity monitoring about 42MB.
- Numbers marked "estimated" come from capacity or cost estimates, including 380 tenants/host (estimated by 760GB÷2GB) and about $8.36/tenant/month (estimated by 80% Savings Plans plus 20% Spot).
- Items marked "to be verified" are points where the code has landed but a fresh measured re-verification has not yet been obtained, or where evidence is insufficient and this guide does not draw a conclusion on its behalf, including a fresh bare-metal re-test of the 100% cross-tenant packet loss after hardening and end-to-end real-machine verification of cross-host live migration.

This solution does not send any anonymized operational metrics to AWS. All metrics and logs are retained within your own AWS account.

## Contributors

- Platform engineering team
- Security and architecture review

# Glossary

This section lists the terms used in this guide. For a general reference of AWS terms, see the AWS glossary.

## Technical protocols and formats

**HMAC**: Hash-based message authentication code, used for external authorization signatures (`POST /external/authz`) and the platform session JWT (HS256).

**JWT**: JSON Web Token. The data-plane platform session token is a platform-issued HS256 (symmetric-key) JWT; control-plane console sign-in uses RS256 JWTs issued by Amazon Cognito.

**JWKS**: JSON Web Key Set, the public key set endpoint exposed by Amazon Cognito, used to verify the control-plane Cognito RS256 JWT signatures.

**Ed25519 device authentication**: The data-plane identity root — OpenClaw's native asymmetric device authentication. The platform backend signs the gateway challenge with the device private key, and the microVM verifies it with the cold-injected public key.

**OAuth 2.0 / PKCE**: The authorization code flow and Proof Key for Code Exchange extension used for control-plane console sign-in (optional, disabled by default).

**WebSocket**: The full-duplex protocol used by the data plane real-time chat path.

## Virtualization technology

**Firecracker**: An AWS open-source lightweight virtualization technology that starts microVMs on top of KVM, with fast startup and strong isolation.

**microVM**: An independent-kernel lightweight virtual machine started by Firecracker; this solution provisions one per tenant.

**KVM**: Kernel-based Virtual Machine, the native virtualization capability of Linux, on which Firecracker runs.

> **Note**
>
> Concepts specific to this solution (tenant, cold injection, read-only golden image, control plane and data plane, the five layers of defense in depth, sample) are defined in "Solution overview — Concepts and definitions" and are not repeated in this glossary, which covers only general technical terms and abbreviations.

## AWS and system terms

**RBAC**: Role-based access control, enabled by default in this solution, in the three tiers viewer, operator, and admin.

**IMDS**: Instance metadata service; this solution drops VM access to it on the host iptables to prevent credential theft.

**ASG**: Amazon EC2 Auto Scaling group, which manages the elastic scaling and rolling rebuild of hosts.

**PITR**: Point-in-Time Recovery, enabled for the control plane metadata tables in this solution, with a default retention of 35 days.

**Guardrail**: Amazon Bedrock Guardrails, the content-layer guardrail of this solution.

# Revisions

| Date    | Version | Change                               |
| ------- | ------- | ------------------------------------ |
| 2026-07 | v1.0    | Initial implementation guide release |
