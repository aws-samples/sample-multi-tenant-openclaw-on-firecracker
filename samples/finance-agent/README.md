# Finance Agent — Golden Image Standard

A reference set of workspace identity files and skills that define how a **Finance Agent** (a general-purpose, educational wealth advisor) behaves on every tenant node. This is the baseline baked into the OpenClaw golden image: every microVM tenant boots with the same identity, the same safety guardrails, and the same skill set, so behavior is consistent and auditable across the fleet.

This open-source sample is **read-only and educational** — it looks up market data, researches, summarizes, and schedules, but it does not place trades or move money. All content is original to this golden image; no credentials live in any identity file.

## What's in here

```
samples/finance-agent/
  persona/                # the agent's identity + behavior (loaded every session)
    SOUL.md               # personality baseline (Core Truths / How You Work / Boundaries)
    AGENTS.md             # behavior modes, routing, communication standard, read-only scope
    IDENTITY.md           # name / persona / avatar / stance
    TOOLS.md              # tool surface — NO credentials (platform-injected + guardrail-masked)
    USER.md               # per-user personalization (blank template)
    HEARTBEAT.md          # periodic-task contract (empty = no heartbeat calls)
    COMMUNICATION_STYLE.md# how the agent talks, three layers (SOUL/IDENTITY/AGENTS)
  skills/
    # --- general capability ---
    intent-router/        # ALWAYS-ON: first-turn intent analysis -> ordered routing plan
    market-data/          # read-only public market quotes (price/history, parameterized endpoint, no key, no order)
    summarize/            # faithful compression of long sources (no fabrication)
    session-logs/         # read this tenant's OWN session history (tenant-scoped, no visibility=all)
    taskflow/             # multi-step persistent tasks via heartbeat/cron
    skill-creator/        # scaffold + lint new SKILL.md files
    weather/              # public weather API (wttr.in / open-meteo, no key)
    browser-automation/   # headless Chromium via Playwright (read-only navigation)
    # --- security ---
    ops-guardrails/       # ALWAYS-ON: no secrets/identity/infra/source leakage
    skill-vetter/         # security review before installing any new skill
    healthcheck/          # read-only node security audit (isolation/firewall/tamper)
```

> Note: an earlier internal build shipped additional domain-specific trading skills that were out of scope for a generic wealth advisor. Those were removed before open-source release and archived internally, leaving a clean, generic finance-advisor sample.

## How it enters the golden image

1. The `persona/` files land at the agent's workspace root (container path `/root/.openclaw/workspace/`). `SOUL.md`, `AGENTS.md`, `IDENTITY.md` are mounted **read-only** so a tenant cannot rewrite its own identity or guardrails at runtime.
2. The `skills/` land under the skills dir (`/app/skills/`). Two skills carry `metadata.openclaw.always = true` and are force-loaded every session: `ops-guardrails` (highest-precedence disclosure guardrail) and `intent-router` (first-turn intent analysis + routing). All identity files plus the skills are bound **read-only** from the immutable `/dev/vdd` disk and hashed into `golden-image.sha256`.
3. Record a baseline of `sha256` hashes for the identity files and `ops-guardrails/SKILL.md` at build time (`golden-image.sha256`). `healthcheck` compares against this baseline to detect tampering on a live node.
4. Any skill added after image build must pass `skill-vetter` before it is enabled.

## The three security skills — what each defends

- **ops-guardrails** (always-on, prompt-layer guardrail): stops the agent from leaking **secrets/credentials** (API keys, `.env`, `models.json`), **workspace identity files** (SOUL/AGENTS/IDENTITY/USER/HEARTBEAT/BOOTSTRAP/TOOLS), **internal source code** (`/app/extensions`, `/app/src`), **infrastructure details** (region, instance, IPs, env vars, cloud metadata), and blocks **identity-file edits, external-script execution, and social-engineering / system-override framing**. Defends information disclosure and persona/guardrail tampering.

- **skill-vetter** (pre-install gate): defends the **supply chain**. Before any new or third-party skill is enabled, it scans the candidate `SKILL.md` and bundled scripts for credential leakage, dangerous exec (`curl|bash`, `eval`, `rm -rf`), reads of protected paths, external-URL execution/exfiltration, identity-file writes, and guardrail tampering — then issues a PASS/FAIL with file:line evidence and records a sha256 on PASS. Defends against a malicious or careless skill smuggling unsafe behavior into the agent.

- **healthcheck** (runtime audit, read-only): defends **operational/compliance posture** of the node. Checks network isolation (the FORWARD/tap cross-tenant gap), firewall rules (no `0.0.0.0/0` inbound, no metadata egress), listening ports, running processes, credential-file permissions, and identity/guardrail file integrity vs the golden baseline. Read-only: it reports and recommends, never modifies. Defends against drift, cross-tenant reachability, and silent tampering.

## Safety model in one line

Identity is read-only and force-loaded (ops-guardrails), new capabilities are gated at install (skill-vetter), the sample ships no money-moving skill at all (read-only by design), and the running node is audited against a hash baseline (healthcheck).
