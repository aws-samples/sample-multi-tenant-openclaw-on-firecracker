# config — per-deployment config & secrets (private)

This layer holds everything that is **specific to one deployment of this sample**:
the OpenClaw runtime config (model provider, base URL, API key) and any per-tenant
secrets. It is the part you fill in for _your_ brand — not something that ships in
the open-source repo.

## What lives here

| Item                                                            | Where it actually comes from                                                        | Notes                                                                       |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `openclaw.json` (model/provider/baseUrl/apiKey)                 | repo root `templates/openclaw.json` (copied from `templates/openclaw.json.example`) | baked into the image by `build-rootfs.sh`; **gitignored** at the repo root  |
| `claw-channel` HMAC secret                                      | minted by the control plane per tenant, injected at **launch** (`launch-vm.sh`)     | never baked into the read-only image — zero credentials in the golden image |
| `MARKET_DATA_API_BASE` (optional market-data endpoint)          | env, injected per tenant if a non-default provider is used                          | the read-only `market-data` skill needs **no** key; this only points it at a provider |

## Why it is its own layer

Keeping config+credentials in `config/` (separate from `persona/`,
`skills/`, `security/`) makes the private/public boundary obvious: the
persona, skills, and security layers are shippable open-source content; **this layer
is per-deployment and must not be committed with real values**.

> **Dev note:** during development this directory is intentionally **not** gitignored
> yet — we keep example/placeholder values here so prompt-injection and guardrail
> interception can be tested against realistic config. Before publishing, switch to
> the `.example` files and enable the gitignore entry (see repo `.gitignore`,
> the commented `samples/*/config/` block).

## Zero-credential golden image

The baked image carries **no live credentials**. Secrets are injected at VM launch
by the control plane (per-tenant, least-privilege). This directory documents the
_shape_ of what gets injected; it is not where production secrets are stored.
