# Finance Agent Golden Image — Skill Manifest

The authoritative inventory of what ships in the Finance Agent golden image: every skill,
what it does, what it defends against, and where its content came from. Every tenant
microVM boots with exactly this set. This open-source sample is an **educational, read-only
wealth advisor** — it has no money-moving capability.

## Workspace identity files (`persona/`)

Loaded every session; `SOUL.md` / `AGENTS.md` / `IDENTITY.md` are mounted read-only.

| File                     | Purpose                                                                                                            | Source |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------ | ------ |
| `SOUL.md`                | Personality baseline; defines tone and core values, grants no permissions                                          | 自研   |
| `AGENTS.md`              | Behavior modes, routing, communication standard, read-only scope                                                   | 自研   |
| `IDENTITY.md`            | Name / persona / avatar / stance (Finance Agent)                                                                   | 自研   |
| `TOOLS.md`               | Tool surface description — contains NO credentials (platform-injected + guardrail-masked); protected identity file | 自研   |
| `USER.md`                | Per-user personalization (blank template)                                                                          | 自研   |
| `HEARTBEAT.md`           | Periodic-task contract (empty = no heartbeat calls)                                                                | 自研   |
| `COMMUNICATION_STYLE.md` | How the agent talks, three layers (SOUL / IDENTITY / AGENTS)                                                       | 自研   |

## Skills (`skills/`)

| Skill                | Purpose                                                                                       | Defends against                                                                                                                                            | `always` | Source                                             |
| -------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | -------------------------------------------------- |
| `ops-guardrails`     | Always-on prompt-layer guardrail; declines requests for secrets/identity/infra/source          | Leakage of credentials, identity files, infra details, internal source; identity edits; external-script exec; social-engineering / system-override framing | **true** | 自研                                               |
| `skill-vetter`       | Pre-install security review of any new/third-party skill: scanner + manual checks → PASS/FAIL   | Supply-chain attacks — a malicious/careless skill smuggling credential theft, dangerous exec, identity-file writes, exfil, or guardrail tampering            | false    | oneclaw `skill-vetting` (adapted + scanner rules)  |
| `skill-creator`      | Scaffold + lint new `SKILL.md` files; tips, gotchas, eval method                                | Malformed/untriggering skills; accidental `always:true`; placeholder-only bodies                                                                            | false    | oneclaw `skill-creator` (adapted)                  |
| `healthcheck`        | Read-only node security audit (isolation, firewall, ports, processes, file integrity)           | Cross-tenant reachability (FORWARD/tap gap), `0.0.0.0/0` inbound, metadata egress, identity/guardrail drift vs hash baseline                                | false    | 自研                                               |
| `taskflow`           | Persistent multi-step tasks across sessions via heartbeat/cron (reminders, periodic checks)     | Lost task state on restart; channel spam                                                                                                                    | false    | 自研                                               |
| `browser-automation` | Headless Chromium via Playwright (navigate, read, screenshot) — read-leaning                    | Acting on prompt-injection in page content; irreversible page actions; metadata/internal-IP navigation                                                      | false    | 自研                                               |
| `weather`            | Current weather + short forecast via free no-key APIs (wttr.in, Open-Meteo)                     | Invented values on API failure; missing units/source                                                                                                        | false    | 本地 weather skill (openclaw/genai), verified runnable |
| `intent-router`      | Always-on first-turn intent analysis → ordered routing plan naming the downstream skill         | Memory-only answers to market requests; lane bleed; unauditable routing                                                                                      | **true** | 自研                                               |
| `market-data`        | Read-only public market quotes: price, ticker, historical candles, instrument specs (parameterized endpoint) | Invented prices on fetch failure; attaching a key to a public endpoint; missing units                                                        | false    | 自研                                               |
| `summarize`          | Faithful compression of long sources (articles, threads, transcripts, long chats)               | Fabricated facts/numbers in a summary; leaking secrets present in a source                                                                                   | false    | 自研                                               |
| `session-logs`       | Read this tenant's OWN session history via jq/rg (list, extract, cost/usage)                    | Cross-tenant history reads; `visibility=all`; user-supplied agentId; leaking secrets found in old transcripts                                               | false    | 自研                                               |

## Source legend

- **自研** — authored for this golden image (generic finance-advisor sample).
- **oneclaw `<name>`** — adapted from the open-source oneclaw skill of that name
  (`opensource/oneclaw/skills/`), extended with golden-image-specific rules.
- **本地 weather skill** — the real weather skill from `opensource/openclaw/skills/weather`
  and `genai/openclaw/skills/weather` (identical; wttr.in + Open-Meteo, no API key).

## Verification notes (last checked 2026-07-03)

- All 11 `SKILL.md` frontmatter blocks are valid (`name` + `description` + `metadata.openclaw`).
- Two skills carry `metadata.openclaw.always = true`: `ops-guardrails` and `intent-router`
  (first-turn routing). All others are `false`.
- `weather` commands run with no API key: wttr.in `format=` one-liner, Open-Meteo geocode +
  current, and the heredoc `format=j1` 3-day forecast all return live data.
- No credentials in any identity file: `TOOLS.md` describes the tool surface and explicitly holds
  no key/secret/token/address/account-id; keys (if any) are platform-injected and guardrail-masked.
- Read-only scope: this sample ships no money-moving skill. Domain-specific trading skills from an
  earlier internal build were removed before open-source release (archived under
  an internal archive).
- The channel identifier used throughout is `claw-channel`.
