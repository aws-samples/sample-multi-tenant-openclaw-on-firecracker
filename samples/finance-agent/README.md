# Finance Agent — Golden Image Sample

A reference set of workspace identity files and skills that define how a **Finance Agent** sample behaves on every tenant node. This is a baseline example baked into the OpenClaw Pool golden image: every microVM tenant boots with the same identity, the same safety guardrails, and the same skill set, so behavior is consistent and auditable across the fleet.

It is a minimal, general-purpose assistant sample — a starting point you can extend with your own skills. All content is original to this sample or pulled from upstream open-source OpenClaw; no credentials live in any identity file.

## What's in here

```
samples/finance-agent/
  persona/                # the agent's identity + behavior (loaded every session)
    SOUL.md               # personality baseline (Core Truths / How You Work / Boundaries)
    AGENTS.md             # behavior modes, communication standard, tool discipline, safety gates
    IDENTITY.md           # name / persona / avatar / stance
    TOOLS.md              # tool surface — NO credentials (platform-injected + guardrail-masked)
    USER.md               # per-user personalization (blank template)
    HEARTBEAT.md          # periodic-task contract (empty = no heartbeat calls)
    COMMUNICATION_STYLE.md# how the agent talks, three layers (SOUL/IDENTITY/AGENTS)
  skills/
    weather/              # public weather API (wttr.in / Open-Meteo, no key) — standard upstream skill
    skill-vetter/         # security review before installing any new skill — standard upstream skill
  security/               # sample security plugins (acl-guard / sentinel-guard / claw-channel)
  config/                 # config templates (values live in .env, never committed)
```

## How it enters the golden image

1. The `persona/` files land at the agent's workspace root (container path `/home/agent/.openclaw/workspace/`). `SOUL.md`, `AGENTS.md`, `IDENTITY.md` are mounted **read-only** so a tenant cannot rewrite its own identity at runtime.
2. The `skills/` land under the skills dir. Both `weather` and `skill-vetter` are loaded on demand (neither is `always:true`). Identity files plus skills are bound **read-only** from the immutable `/dev/vdd` disk and hashed into `golden-image.sha256`.
3. A baseline of `sha256` hashes for the identity files is recorded at build time (`golden-image.sha256`), so tampering on a live node can be detected.
4. Any skill added after image build must pass `skill-vetter` before it is enabled.

## The skills

- **weather** — read-only current weather and short forecasts via free, no-key public APIs (wttr.in and Open-Meteo). Standard upstream OpenClaw skill.
- **skill-vetter** — a security-first review checklist that runs before any new or third-party skill is installed or enabled: it flags credential leakage, dangerous exec, reads of protected paths, external-URL execution, identity-file writes, and prompt injection, then issues a PASS / FAIL. Standard upstream skill from the OpenClaw skills-security set.

## Safety model in one line

Identity is read-only and hashed into a baseline; new capabilities are gated at install (`skill-vetter`); the agent is informational and educational and takes no irreversible action without an explicit confirm.
