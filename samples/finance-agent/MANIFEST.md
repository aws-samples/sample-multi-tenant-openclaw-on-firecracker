# Finance Agent Golden Image — Skill Manifest

The authoritative inventory of what ships in the Finance Agent golden image: every skill,
what it does, what it defends against, and where its content came from. Every tenant
microVM boots with exactly this set.

## Workspace identity files (`persona/`)

Loaded every session; `SOUL.md` / `AGENTS.md` / `IDENTITY.md` are mounted read-only.

| File                     | Purpose                                                                                                            | Source |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------ | ------ |
| `SOUL.md`                | Personality baseline; defines tone and core values, grants no permissions                                          | 自研   |
| `AGENTS.md`              | Behavior modes, communication standard, tool discipline, privacy/safety gates                                      | 自研   |
| `IDENTITY.md`            | Name / persona / avatar / stance (Finance Agent)                                                                   | 自研   |
| `TOOLS.md`               | Tool surface description — contains NO credentials (platform-injected + guardrail-masked); protected identity file | 自研   |
| `USER.md`                | Per-user personalization (blank template)                                                                          | 自研   |
| `HEARTBEAT.md`           | Periodic-task contract (empty = no heartbeat calls)                                                                | 自研   |
| `COMMUNICATION_STYLE.md` | How the agent talks, three layers (SOUL / IDENTITY / AGENTS)                                                       | 自研   |

## Skills (`skills/`)

| Skill          | Purpose                                                                                          | Defends against                                                                                                                     | `always` | Source                                            |
| -------------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------- |
| `weather`      | Current weather + short forecast via free no-key APIs (wttr.in, Open-Meteo)                      | Invented values on API failure; missing units/source                                                                               | false    | Standard `openclaw/openclaw` `skills/weather`     |
| `skill-vetter` | Security review before enabling any new/third-party skill → PASS / FAIL verdict                  | Supply-chain risk — a malicious/careless skill smuggling credential theft, dangerous exec, identity-file writes, exfil, or tampering | false    | Standard `openclaw-skills-security` `skills/skill-vetter` |

## Source legend

- **自研** — authored for this golden image.
- **Standard** — pulled verbatim from the upstream open-source OpenClaw skill of that name.

## Verification notes

- Both `SKILL.md` frontmatter blocks are valid (`name` + `description` + `metadata`).
- Neither skill carries `metadata.openclaw.always = true`; both are loaded on demand.
- `weather` commands run with no API key: wttr.in `format=` one-liner and Open-Meteo
  geocode + current both return live public data.
- `skill-vetter` provides a manual, security-first pre-install review checklist.
- No credentials in any identity file: `TOOLS.md` describes the tool surface and explicitly
  holds no key/secret/token; any keys are platform-injected and guardrail-masked.
- The channel identifier used throughout is `claw-channel`.
