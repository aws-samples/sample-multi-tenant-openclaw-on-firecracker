// sentinel-guard — comprehensive code-enforced runtime security layer for the
// golden image.  This is the "answer-side" (L3) enforcement that runs
// INSIDE the agent microVM, vetoing dangerous tool calls before they execute,
// scrubbing secrets out of model output, and rejecting prompt-injection input.
//
// WHY a second plugin alongside acl-guard?
//   acl-guard is a tight secret/IMDS exfil deny-list (priority 1000, runs first).
//   sentinel-guard is the broad mechanism set: path-prefix protection with
//   traversal-safe normalization, identity-file protection (read + outbound
//   attachment + reply-text), reverse-shell / destructive / privilege-escalation
//   command rules, CIDR-aware SSRF, three-layer secret redaction on output, a
//   sliding-window behaviour monitor, and prompt-injection screening.  Both
//   fail closed and both veto independently — defence in depth.
//
// Hook contract (verified against acl-guard, which is the working baseline in
// this image: OpenClaw fires `before_tool_call` with { toolName, params, ctx }
// and a returned { block:true, blockReason } is a hard runtime veto):
//   before_tool_call  → veto dangerous exec / file / network / message calls
//   after_tool_call   → behaviour-anomaly monitor + skill-content scan
//   llm_input         → reject prompt injection (best-effort; depends on host)
//   llm_output        → redact leaked secrets in-place (best-effort)
//   message_sending   → last-mile redact secrets + identity-file references
//
// Brand-neutral: every brand string, identity-file name, and config path is
// our own (generic / brand-neutral).  No third-party brand text, no credentials,
// no API keys.  Mechanisms are re-implemented from documented behaviour, not
// copied from any third-party source.
//
// Design constraints: single dependency-free .js (Node stdlib only), mirrors
// how acl-guard ships in this golden image (no TypeScript build step). Every
// guard is best-effort and never throws into the host — a guard failure must
// not crash the gateway, and the safe failure is "still blocking".

import { appendFileSync, mkdirSync } from "node:fs";
import {
  dirname,
  basename,
  normalize,
  resolve as resolvePath,
  join,
} from "node:path";
import { homedir } from "node:os";

// ---------------------------------------------------------------------------
// Audit log (fail-open on logging, fail-closed on enforcement)
// ---------------------------------------------------------------------------

const LOG_PATH =
  process.env.OPENCLAW_SENTINEL_LOG ||
  "/home/agent/.openclaw/logs/sentinel-guard.log";

function audit(entry) {
  try {
    mkdirSync(dirname(LOG_PATH), { recursive: true });
    appendFileSync(
      LOG_PATH,
      JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n",
    );
  } catch (_e) {
    // Never let a logging failure crash the hook. We lose the audit line but
    // the block (if any) still stands — failing closed is the safe outcome.
  }
}

function truncate(s, n = 240) {
  const str = String(s ?? "");
  return str.length > n ? str.slice(0, n) + "…" : str;
}

// ---------------------------------------------------------------------------
// Tool name sets — which tools each guard inspects
// ---------------------------------------------------------------------------

const EXEC_TOOLS = new Set(["exec", "bash", "shell"]);
const READ_TOOLS = new Set(["read", "read_file"]);
const WRITE_TOOLS = new Set(["write", "fs_write", "write_file", "create_file"]);
const DELETE_TOOLS = new Set(["fs_delete", "delete_file"]);
const FILE_TOOLS = new Set([...READ_TOOLS, ...WRITE_TOOLS, ...DELETE_TOOLS]);
const NETWORK_TOOLS = new Set(["fetch", "curl", "http", "web_search"]);
const MESSAGE_TOOLS = new Set(["message"]);
const DEFERRED_EXEC_TOOLS = new Set(["cron", "sessions_spawn"]);

// ---------------------------------------------------------------------------
// Identity files — the persona/guardrail docs injected into the system
// prompt.  These must never be exfiltrated by name (read, attached, or echoed
// in a reply).  Matches the files baked into samples/finance-agent/workspace-files.
// (Write-protection is already enforced one layer down by the read-only
// /dev/vdd bind-mount; this is the read/exfil backstop at the tool layer.)
// ---------------------------------------------------------------------------

const IDENTITY_FILE_BASENAMES = new Set([
  "soul.md",
  "agents.md",
  "identity.md",
  "user.md",
  "heartbeat.md",
  "communication_style.md",
  "tools.md",
  "bootstrap.md",
  "memory.md",
]);

function isIdentityFile(fileNameOrPath) {
  if (!fileNameOrPath || typeof fileNameOrPath !== "string") return false;
  return IDENTITY_FILE_BASENAMES.has(
    basename(fileNameOrPath.trim()).toLowerCase(),
  );
}

// ---------------------------------------------------------------------------
// Param extraction helpers
// ---------------------------------------------------------------------------

function extractFilePath(params) {
  for (const key of [
    "path",
    "file_path",
    "filepath",
    "filename",
    "file",
    "target",
  ]) {
    const v = params?.[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return undefined;
}

function extractCommand(params) {
  for (const key of ["command", "cmd", "script"]) {
    const v = params?.[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return undefined;
}

function fileOperation(toolName) {
  if (READ_TOOLS.has(toolName)) return "read";
  if (WRITE_TOOLS.has(toolName)) return "write";
  if (DELETE_TOOLS.has(toolName)) return "delete";
  return undefined;
}

// Message tool can attach local files via several param names. Collect the
// non-URL ones so we can refuse sending protected files as attachments.
function extractMessageFilePaths(params) {
  const out = [];
  for (const key of [
    "media",
    "path",
    "filePath",
    "file_path",
    "filepath",
    "file",
    "attachment",
  ]) {
    const v = params?.[key];
    if (
      typeof v === "string" &&
      v.trim() &&
      !v.startsWith("http://") &&
      !v.startsWith("https://")
    ) {
      out.push(v.trim());
    }
  }
  return out;
}

// Deferred-execution tools schedule a future agent turn whose prompt could
// instruct reading secrets. Pull the free-text payload so we can pre-screen it.
function extractDeferredPayload(toolName, params) {
  if (toolName === "cron") {
    const payload = params?.payload;
    const msg = (payload && payload.message) ?? params?.message;
    if (typeof msg === "string" && msg.trim()) return msg.trim();
  }
  if (toolName === "sessions_spawn") {
    for (const key of ["task", "message", "prompt", "input"]) {
      const v = params?.[key];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Command guard — shell-command deny rules
//
// Categories: credential-theft, data-exfiltration, destructive, reverse-shell,
// privilege-escalation. Re-implemented from documented behaviour, brand-neutral.
// ---------------------------------------------------------------------------

const COMMAND_RULES = [
  // ── Credential theft ──────────────────────────────────────────────
  {
    id: "cred-ssh",
    cat: "credential-theft",
    sev: "critical",
    why: "reading/accessing SSH key directory",
    rx: /\b(cat|head|tail|less|more|cp|scp|tar|strings|xxd|od|hexdump|vi|vim|nano|bat)\b.*~?\/?\.ssh(\/|\b)/i,
  },
  {
    id: "cred-aws",
    cat: "credential-theft",
    sev: "critical",
    why: "reading AWS credentials/config",
    rx: /\b(cat|head|tail|less|more|cp|scp|tar|strings)\b.*~?\/?\.aws\/(credentials|config)/i,
  },
  {
    id: "cred-gnupg",
    cat: "credential-theft",
    sev: "critical",
    why: "reading GPG key directory",
    rx: /\b(cat|head|tail|less|more|cp|scp|tar|mv|strings)\b.*~?\/?\.gnupg(\/|\b)/i,
  },
  {
    id: "cred-kube",
    cat: "credential-theft",
    sev: "critical",
    why: "reading Kubernetes config",
    rx: /\b(cat|head|tail|less|more|cp|scp|tar|mv|strings)\b.*~?\/?\.kube\/config/i,
  },
  {
    id: "cred-docker",
    cat: "credential-theft",
    sev: "high",
    why: "reading Docker credentials",
    rx: /\b(cat|head|tail|less|more|cp|scp|tar|mv|strings)\b.*~?\/?\.docker\/config\.json/i,
  },
  {
    id: "cred-dotenv",
    cat: "credential-theft",
    sev: "high",
    why: "reading .env file",
    rx: /\b(cat|head|tail|less|more|cp|scp|tar|mv|strings)\b.*\.env(\.|\b)/i,
  },
  {
    id: "cred-git",
    cat: "credential-theft",
    sev: "high",
    why: "reading git credentials",
    rx: /\b(cat|head|tail|less|more|cp|scp|tar|mv|strings)\b.*\.git(config|-credentials)/i,
  },
  {
    id: "cred-history",
    cat: "credential-theft",
    sev: "high",
    why: "reading shell history (may contain secrets)",
    rx: /\b(cat|head|tail|less|more|cp|scp|tar|strings)\b.*\.(bash_history|zsh_history|history)\b/i,
  },
  {
    id: "cred-openclaw-config",
    cat: "credential-theft",
    sev: "critical",
    why: "reading agent runtime config (contains API keys/tokens)",
    rx: /\b(cat|head|tail|less|more|cp|scp|tar|bat|strings|xxd|od|hexdump|vi|vim|nano)\b.*\.openclaw\/[\w.-]*\.json\b/i,
  },
  {
    id: "cred-openclaw-state",
    cat: "credential-theft",
    sev: "critical",
    why: "accessing agent state dir (credentials/sessions/exec-approvals)",
    // Command list widened beyond cat/strings: jq, rg, awk, sed, perl, python,
    // ruby, node, xxd, od all read files too — and jq/rg are exactly what the
    // session-logs skill teaches, so a literal cross-agent path like
    // .openclaw/agents/<other>/sessions must be caught for them as well. The
    // skill's own reads use the $SESSION_DIR variable (no literal
    // ".openclaw/agents" substring), so legitimate self-reads are unaffected.
    rx: /\b(ls|find|cat|head|tail|less|more|cp|scp|tar|bat|strings|jq|rg|grep|awk|sed|perl|python[23]?|ruby|node|xxd|od|hexdump|base64)\b.*\.openclaw\/(credentials|exec-approvals|sessions|auth|agents)\b/i,
  },
  {
    id: "cred-env-dump",
    cat: "credential-theft",
    sev: "medium",
    why: "dumping all environment variables",
    rx: /(^|[\s;&|`(])(env|printenv|set)([\s;&|`)]|$)|\b(export\s+-p|declare\s+-x)\b|\/proc\/self\/environ|\bpython[23]?\s+-c\s+.*os\.environ|\bnode\s+-[ep]\s+.*process\.env/i,
  },
  {
    id: "cred-env-grep",
    cat: "credential-theft",
    sev: "high",
    why: "scanning env vars for secrets",
    rx: /\b(env|printenv)\b.*\|\s*grep\b.*\b(secret|token|password|key|credential|api.?key)/i,
  },
  {
    id: "cred-proc-environ",
    cat: "credential-theft",
    sev: "high",
    why: "reading /proc/<pid>/environ",
    rx: /\/proc\/[^/\s]+\/environ/i,
  },

  // ── Data exfiltration ─────────────────────────────────────────────
  {
    id: "exfil-curl-upload",
    cat: "data-exfiltration",
    sev: "critical",
    why: "uploading file content via curl",
    rx: /\bcurl\b.*(-d\s*@|-F\s*['"]*file=@|--data-binary\s*@|--upload-file)/i,
  },
  {
    id: "exfil-wget-post",
    cat: "data-exfiltration",
    sev: "critical",
    why: "sending data via wget POST",
    rx: /\bwget\b.*--post-(data|file)/i,
  },
  {
    id: "exfil-base64-pipe",
    cat: "data-exfiltration",
    sev: "high",
    why: "base64/hex encode piped to a network tool",
    rx: /\b(base64|xxd)\b.*\|\s*(curl|wget|nc|ncat|socat)\b/i,
  },

  // ── Destructive ───────────────────────────────────────────────────
  {
    id: "destr-rm-root",
    cat: "destructive",
    sev: "critical",
    why: "recursive delete of root filesystem",
    // Matches rm targeting / with recursive+force in EITHER flag order
    // (-rf or -fr, the old rule forced r-before-f and missed -fr) as well as
    // the long-option spelling (--recursive --force, any order).
    rx: /\brm\b.*(-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*|--recursive|--force).*\s+\/(\*|\s|$)/i,
  },
  {
    id: "destr-mkfs",
    cat: "destructive",
    sev: "critical",
    why: "formatting a filesystem",
    rx: /\bmkfs\b/i,
  },
  {
    id: "destr-dd-dev",
    cat: "destructive",
    sev: "critical",
    why: "overwriting a block device with dd",
    rx: /\bdd\b.*if=\/dev\/(zero|random|urandom).*of=\/dev\//i,
  },
  {
    id: "destr-fork-bomb",
    cat: "destructive",
    sev: "critical",
    why: "fork bomb",
    rx: /:\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:/,
  },

  // ── Reverse shells ────────────────────────────────────────────────
  {
    id: "rshell-bash-tcp",
    cat: "reverse-shell",
    sev: "critical",
    why: "bash reverse shell via /dev/tcp",
    rx: /bash\s+-i\s*>?\s*&?\s*\/dev\/tcp\//i,
  },
  {
    id: "rshell-python-socket",
    cat: "reverse-shell",
    sev: "critical",
    why: "python reverse shell via socket",
    rx: /python[23]?\s+-c\s+.*import\s+socket/i,
  },
  {
    id: "rshell-perl-socket",
    cat: "reverse-shell",
    sev: "critical",
    why: "perl reverse shell via socket",
    rx: /perl\s+-e\s+.*socket/i,
  },
  {
    id: "rshell-nc-exec",
    cat: "reverse-shell",
    sev: "critical",
    why: "netcat reverse shell / exec flag",
    // `ncat?` only matched "nca"/"ncat" — the bare `nc -e /bin/sh` (the most
    // common netcat reverse shell) slipped through. `nc(at)?` matches both
    // `nc` and `ncat`.
    rx: /\bnc(at)?\b.*(-e\b|-c\s*\/bin\/(sh|bash))/i,
  },
  {
    id: "rshell-socat",
    cat: "reverse-shell",
    sev: "critical",
    why: "socat reverse shell",
    rx: /\bsocat\b.*EXEC.*\/bin\/(sh|bash)/i,
  },

  // ── Privilege escalation ──────────────────────────────────────────
  {
    id: "priv-chmod-root",
    cat: "privilege-escalation",
    sev: "high",
    why: "world-writable permissions on a root path",
    rx: /\bchmod\b.*777\s+\//i,
  },
  {
    id: "priv-sudo-su",
    cat: "privilege-escalation",
    sev: "high",
    why: "attempting to escalate to root",
    rx: /\b(sudo\s+su|sudo\s+-i|sudo\s+bash|su\s+-\s*$|su\s+root)\b/i,
  },
  {
    id: "priv-suid",
    cat: "privilege-escalation",
    sev: "critical",
    why: "setting SUID/SGID bits",
    rx: /\bchmod\b.*[ug]\+s\b/i,
  },

  // ── Fund-movement actions (code-layer veto — DEVIL R2 fix) ─────────
  // The agent must NOT execute signed money moves (withdraw / transfer /
  // sub-account move) via an exchange CLI unless the command carries an
  // explicit --dry-run / preview, OR a confirm token the platform injects
  // out-of-band. This is a HARD veto at before_tool_call, NOT prompt self-
  // discipline — so a prompt-injected "transfer to attacker addr" is blocked
  // at the tool layer even if the model is fooled. (Until exchange-cli is really
  // wired this is belt-and-suspenders; it must be in place BEFORE that day.)
  {
    id: "fund-withdraw",
    cat: "fund-action",
    sev: "critical",
    why: "signed withdrawal without --dry-run / confirm token (irreversible money movement)",
    // exchange-cli withdraw ... / any *-cli withdraw, unless --dry-run or --confirm-token=
    rx: /\b\S*-?cli\b.*\bwithdraw(al)?\b(?!.*(--dry-run|--preview|--confirm-token=))/i,
  },
  {
    id: "fund-transfer",
    cat: "fund-action",
    sev: "critical",
    why: "signed transfer / sub-account move without --dry-run / confirm token",
    rx: /\b\S*-?cli\b.*\b(transfer|sub-?account-?move|universal-?transfer)\b(?!.*(--dry-run|--preview|--confirm-token=))/i,
  },
  {
    id: "fund-order-live",
    cat: "fund-action",
    sev: "high",
    why: "live order placement without --dry-run / testnet / confirm token",
    rx: /\b\S*-?cli\b.*\b(place-?order|create-?order|order\s+(create|place))\b(?!.*(--dry-run|--preview|--testnet|--confirm-token=))/i,
  },
];

// Skill-tampering: refuse drive-by skill installs / SKILL.md overwrites that
// could plant injected instructions in the agent's own skill tree.
const SKILL_INSTALL_RULES = [
  {
    id: "skill-install-fetch",
    cat: "skill-tamper",
    sev: "critical",
    why: "downloading content into a skills directory",
    rx: /\b(curl|wget)\b[\s\S]*?(>\s*|--output\s+|-[oO]\s*)[\s\S]*?(skills\/|SKILL\.md)/i,
  },
  {
    id: "skill-install-write",
    cat: "skill-tamper",
    sev: "critical",
    why: "creating/overwriting a SKILL.md file",
    rx: /\b(cat|echo|printf|tee)\b[\s\S]*?>[\s\S]*?SKILL\.md\b/i,
  },
  {
    id: "skill-install-clone",
    cat: "skill-tamper",
    sev: "high",
    why: "cloning a repository into a skills directory",
    rx: /\bgit\s+clone\b[\s\S]*?skills\//i,
  },
];

// Strip shell comments so an agent annotating intent ("# load .env") doesn't
// false-trip a credential rule. Lightweight: respects single/double quotes and
// backslash escapes; not a full shell parser.
function stripShellComments(command) {
  if (!command) return "";
  let out = "";
  let i = 0;
  const s = command;
  while (i < s.length) {
    const c = s[i];
    if (c === "\\" && i + 1 < s.length) {
      out += c + s[i + 1];
      i += 2;
      continue;
    }
    if (c === "'") {
      out += c;
      i++;
      while (i < s.length && s[i] !== "'") {
        out += s[i];
        i++;
      }
      if (i < s.length) {
        out += s[i];
        i++;
      }
      continue;
    }
    if (c === '"') {
      out += c;
      i++;
      while (i < s.length) {
        if (s[i] === "\\" && i + 1 < s.length) {
          out += s[i] + s[i + 1];
          i += 2;
          continue;
        }
        out += s[i];
        if (s[i] === '"') {
          i++;
          break;
        }
        i++;
      }
      continue;
    }
    if (c === "#" && (i === 0 || /[\s;&|(]/.test(s[i - 1] ?? ""))) {
      while (i < s.length && s[i] !== "\n") i++;
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

function checkCommand(command) {
  if (!command || typeof command !== "string") return null;
  const normalized = stripShellComments(command).replace(/\s+/g, " ").trim();
  for (const rule of [...COMMAND_RULES, ...SKILL_INSTALL_RULES]) {
    if (rule.rx.test(normalized)) return rule;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Path guard — operation-aware protected-path prefixes.  ~ expansion + path
// normalization defeats `../` traversal bypasses that a pure string match
// would miss (e.g. /home/agent/.openclaw/workspace/../../.ssh/id_rsa).
// ---------------------------------------------------------------------------

// Home-relative protected sub-paths. We expand these against EVERY plausible
// agent-home base — the gateway's resolved os.homedir() plus the canonical
// "/home/agent" — so the guard holds whether the runtime resolves home one way
// or the other, and whether the tool passes an absolute or ~-relative path.
const HOME_SUBRULES = [
  { id: "path-ssh", sev: "critical", why: "SSH keys/config", rel: ".ssh" },
  { id: "path-gnupg", sev: "critical", why: "GPG keys", rel: ".gnupg" },
  { id: "path-aws", sev: "critical", why: "AWS credentials", rel: ".aws" },
  { id: "path-kube", sev: "critical", why: "Kubernetes config", rel: ".kube" },
  { id: "path-docker", sev: "high", why: "Docker credentials", rel: ".docker" },
  {
    id: "path-oc-cred",
    sev: "critical",
    why: "agent credentials dir",
    rel: ".openclaw/credentials",
  },
  {
    id: "path-oc-approvals",
    sev: "critical",
    why: "agent exec-approvals",
    rel: ".openclaw/exec-approvals",
  },
  {
    id: "path-oc-env",
    sev: "critical",
    why: "agent injected .env",
    rel: ".openclaw/.env",
  },
  {
    id: "path-oc-config",
    sev: "critical",
    why: "agent main config (API keys/tokens)",
    rel: ".openclaw/openclaw.json",
  },
  {
    id: "path-oc-agents",
    sev: "critical",
    why: "per-agent auth dir",
    rel: ".openclaw/agents",
  },
];

const ABSOLUTE_RULES = [
  {
    id: "path-etc-shadow",
    sev: "critical",
    why: "system shadow file",
    prefix: "/etc/shadow",
  },
  {
    id: "path-etc-passwd",
    sev: "high",
    why: "system passwd file",
    prefix: "/etc/passwd",
  },
  {
    id: "path-etc-sudoers",
    sev: "high",
    why: "sudoers config",
    prefix: "/etc/sudoers",
  },
];

function buildPathRules() {
  const bases = [...new Set([homedir(), "/home/agent"])];
  const rules = [];
  for (const base of bases) {
    for (const r of HOME_SUBRULES) {
      rules.push({
        id: r.id,
        sev: r.sev,
        why: r.why,
        prefix: normalize(join(base, r.rel)),
      });
    }
  }
  for (const r of ABSOLUTE_RULES) rules.push({ ...r });
  return rules;
}

const PATH_RULES = buildPathRules();

function normalizePath(p) {
  const expanded = p.startsWith("~")
    ? join(homedir(), p.slice(1))
    : resolvePath(p);
  return normalize(expanded);
}

function checkPath(filePath, operation) {
  if (!filePath || typeof filePath !== "string") return null;
  // Identity files: protect read/exfil regardless of directory.
  if (isIdentityFile(filePath)) {
    return {
      id: "identity-file",
      sev: "high",
      why: `protected identity file (${operation}: ${basename(filePath)})`,
    };
  }
  const norm = normalizePath(filePath);
  for (const rule of PATH_RULES) {
    if (
      norm === rule.prefix ||
      norm.startsWith(rule.prefix + "/") ||
      norm.startsWith(rule.prefix)
    ) {
      return {
        id: rule.id,
        sev: rule.sev,
        why: `${rule.why} (${operation}: ${filePath})`,
      };
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Network / SSRF guard — CIDR-aware private-range + cloud-metadata blocking.
// ---------------------------------------------------------------------------

function isIPv4(host) {
  return /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host);
}
function ipv4ToNum(ip) {
  const p = ip.split(".").map(Number);
  return ((p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]) >>> 0;
}
function inCIDR(ip, cidr) {
  const [base, bits] = cidr.split("/");
  const mask = (~0 << (32 - Number(bits))) >>> 0;
  return (ipv4ToNum(ip) & mask) === (ipv4ToNum(base) & mask);
}

const NETWORK_RULES = [
  {
    id: "ssrf-metadata-aws",
    sev: "critical",
    why: "AWS/Azure IMDS endpoint (169.254.169.254)",
    test: (h) => h === "169.254.169.254" || h === "169.254.169.253",
  },
  {
    id: "ssrf-metadata-gcp",
    sev: "critical",
    why: "GCP metadata endpoint",
    test: (h) =>
      h === "metadata.google.internal" || h === "metadata.google.com",
  },
  {
    id: "ssrf-metadata-ali",
    sev: "critical",
    why: "cloud IMDS endpoint (100.100.100.200)",
    test: (h) => h === "100.100.100.200",
  },
  {
    id: "ssrf-private-10",
    sev: "high",
    why: "private network 10.0.0.0/8",
    test: (h) => isIPv4(h) && inCIDR(h, "10.0.0.0/8"),
  },
  {
    id: "ssrf-private-172",
    sev: "high",
    why: "private network 172.16.0.0/12",
    test: (h) => isIPv4(h) && inCIDR(h, "172.16.0.0/12"),
  },
  {
    id: "ssrf-private-192",
    sev: "high",
    why: "private network 192.168.0.0/16",
    test: (h) => isIPv4(h) && inCIDR(h, "192.168.0.0/16"),
  },
  {
    id: "ssrf-link-local",
    sev: "high",
    why: "link-local 169.254.0.0/16",
    test: (h) => isIPv4(h) && inCIDR(h, "169.254.0.0/16"),
  },
  {
    id: "ssrf-loopback",
    sev: "high",
    why: "loopback address",
    test: (h) =>
      h === "localhost" ||
      h === "[::1]" ||
      (isIPv4(h) && inCIDR(h, "127.0.0.0/8")),
  },
  {
    id: "ssrf-internal-host",
    sev: "medium",
    why: "internal hostname (.internal/.local/.corp/.lan)",
    test: (h) => /\.(internal|local|corp|lan|intra|private)$/i.test(h),
  },
];

function hostnameOf(urlStr) {
  try {
    return new URL(urlStr).hostname.toLowerCase();
  } catch {
    return null;
  }
}

function checkUrl(url) {
  const host = hostnameOf(url);
  if (!host) return null;
  for (const rule of NETWORK_RULES) {
    if (rule.test(host))
      return { id: rule.id, sev: rule.sev, why: `${rule.why} — ${url}` };
  }
  return null;
}

function checkNetworkParams(params) {
  for (const key of ["url", "endpoint", "uri", "href", "target", "address"]) {
    const v = params?.[key];
    if (typeof v === "string") {
      const r = checkUrl(v);
      if (r) return r;
    }
  }
  return null;
}

const URL_IN_CMD = /https?:\/\/[^\s'"<>|;]+/gi;
function checkCommandUrls(cmd) {
  const matches = String(cmd).match(URL_IN_CMD) ?? [];
  for (const u of matches) {
    const r = checkUrl(u);
    if (r) return r;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Secret guard — three-layer detection + redaction for model output.
//   L1 signature  · L2 context (variable-name implies a credential) · L3 entropy
// This is the code-level half of the "two-way masking" posture: even a
// jailbroken model cannot leak a secret it managed to read, because the bytes
// are scrubbed on the way out.
// ---------------------------------------------------------------------------

const L1_RULES = [
  {
    id: "aws-akid",
    label: "AWS Access Key ID",
    rx: /\bAKIA[0-9A-Z]{16}\b/,
    as: "[REDACTED:AWS_KEY]",
  },
  {
    id: "aws-secret",
    label: "AWS Secret Key",
    rx: /(?<=(?:aws_secret_access_key|secret_?key)\s*[:=]\s*['"]?)[A-Za-z0-9/+=]{40}(?=['"]?\s)/i,
    as: "[REDACTED:AWS_SECRET]",
  },
  {
    id: "pem-key",
    label: "Private Key (PEM)",
    rx: /-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----/,
    as: "[REDACTED:PRIVATE_KEY]",
  },
  {
    id: "github-pat",
    label: "GitHub PAT",
    rx: /\bgh[ps]_[A-Za-z0-9_]{36,}\b/,
    as: "[REDACTED:GITHUB_PAT]",
  },
  {
    id: "github-fine",
    label: "GitHub Fine-Grained PAT",
    rx: /\bgithub_pat_[A-Za-z0-9_]{22,}\b/,
    as: "[REDACTED:GITHUB_PAT]",
  },
  {
    id: "gitlab",
    label: "GitLab Token",
    rx: /\bglpat-[A-Za-z0-9-]{20,}\b/,
    as: "[REDACTED:GITLAB_TOKEN]",
  },
  {
    id: "slack",
    label: "Slack Token",
    rx: /\bxox[bporas]-[A-Za-z0-9-]{10,}\b/,
    as: "[REDACTED:SLACK_TOKEN]",
  },
  {
    id: "npm",
    label: "NPM Token",
    rx: /\bnpm_[A-Za-z0-9]{36}\b/,
    as: "[REDACTED:NPM_TOKEN]",
  },
  {
    id: "jwt",
    label: "JWT",
    rx: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/,
    as: "[REDACTED:JWT]",
  },
  {
    id: "db-conn",
    label: "DB connection string",
    rx: /(?:mongodb|postgres|postgresql|mysql|redis|amqp):\/\/[^:]+:[^@\s]+@[^\s]+/i,
    as: "[REDACTED:DB_CONNECTION]",
  },
  // Generic exchange/API key shape: a 64-hex/base62 token near a "key/secret"
  // word.  Brand-neutral — catches a leaked exchange-style API key/secret without
  // naming any exchange.
  {
    id: "exchange-api-key",
    label: "Exchange API key/secret",
    rx: /\b[A-Za-z0-9]{64}\b(?=[\s\S]{0,40}(?:api[_-]?key|api[_-]?secret|secret))/i,
    as: "[REDACTED:EXCHANGE_KEY]",
  },
  {
    id: "bearer",
    label: "Bearer Token",
    rx: /\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b/,
    as: "[REDACTED:BEARER_TOKEN]",
  },
];

const SENSITIVE_NAME =
  "token|secret|password|passwd|pwd|api_?key|apikey|access_?key|auth_?token|" +
  "private_?key|credential|bearer|app_?key|app_?secret|client_?secret|" +
  "signing_?key|encryption_?key|master_?key|webhook_?secret|bot_?token";

const L2_RULES = [
  {
    id: "env-unquoted",
    label: "secret in env var (unquoted)",
    rx: new RegExp(
      `(?:^|\\n|\\s)([A-Za-z0-9_]*(?:${SENSITIVE_NAME})[A-Za-z0-9_]*)\\s*=\\s*([^\\s'"$\`][^\\s]{7,})`,
      "i",
    ),
    as: "[REDACTED:ENV_SECRET]",
  },
  {
    id: "assign-quoted",
    label: "secret in assignment (quoted)",
    rx: new RegExp(
      `(?:^|\\n|\\s|export\\s+)([A-Za-z0-9_]*(?:${SENSITIVE_NAME})[A-Za-z0-9_]*)\\s*[:=]\\s*(['"])([^'"]{8,})\\2`,
      "i",
    ),
    as: "[REDACTED:SECRET]",
  },
  {
    id: "json-secret",
    label: "secret in JSON property",
    rx: new RegExp(
      `"([^"]*(?:${SENSITIVE_NAME})[^"]*)"\\s*:\\s*"([^"]{8,})"`,
      "i",
    ),
    as: "[REDACTED:JSON_SECRET]",
  },
  {
    id: "http-header",
    label: "secret in HTTP header",
    rx: /(?:X-Api-Key|X-Auth-Token|Authorization|X-Secret-Key|X-Access-Token)\s*:\s*([^\s]{8,})/i,
    as: "[REDACTED:HTTP_HEADER_SECRET]",
  },
];

const ENTROPY_CTX =
  /\b(?:key|token|secret|password|credential|auth|bearer|api|private|signing|encryption|webhook|bot|app|client|access|session|cookie)\b/i;
const ENTROPY_CANDIDATE = new RegExp(
  `(?:${ENTROPY_CTX.source})[^\\n]{0,30}?[:=]\\s*['"]?([A-Za-z0-9+/=\\-._~]{12,256})`,
  "gi",
);

function shannonEntropy(str) {
  if (!str) return 0;
  const freq = new Map();
  for (const ch of str) freq.set(ch, (freq.get(ch) ?? 0) + 1);
  let e = 0;
  for (const count of freq.values()) {
    const p = count / str.length;
    if (p > 0) e -= p * Math.log2(p);
  }
  return e;
}

function looksFalsePositive(v) {
  if (/^\d+$/.test(v)) return true;
  if (/^\/[\w/.-]+$/.test(v)) return true;
  if (/^https?:\/\/[^@]*$/.test(v)) return true;
  if (/^\/\/[a-zA-Z0-9]/.test(v)) return true;
  if (/^\d+\.\d+\.\d+/.test(v)) return true;
  if (new Set(v).size <= 3) return true;
  if (/^#?[0-9a-f]{6}$/i.test(v)) return true;
  return false;
}

const ENTROPY_THRESHOLD = 3.5;

function hasSecret(text) {
  if (!text || typeof text !== "string") return false;
  for (const r of [...L1_RULES, ...L2_RULES]) if (r.rx.test(text)) return true;
  const re = new RegExp(ENTROPY_CANDIDATE.source, ENTROPY_CANDIDATE.flags);
  let m;
  while ((m = re.exec(text)) !== null) {
    const v = m[1];
    if (
      v &&
      v.length >= 12 &&
      !looksFalsePositive(v) &&
      shannonEntropy(v) >= ENTROPY_THRESHOLD
    )
      return true;
    if (m[0].length === 0) re.lastIndex++;
  }
  return false;
}

function redactSecrets(text) {
  if (!text || typeof text !== "string") return text;
  let out = text;
  for (const r of [...L1_RULES, ...L2_RULES]) {
    const re = new RegExp(
      r.rx.source,
      r.rx.flags.includes("g") ? r.rx.flags : r.rx.flags + "g",
    );
    out = out.replace(re, r.as);
  }
  const re = new RegExp(ENTROPY_CANDIDATE.source, ENTROPY_CANDIDATE.flags);
  out = out.replace(re, (full, v) => {
    if (
      v &&
      v.length >= 12 &&
      !looksFalsePositive(v) &&
      shannonEntropy(v) >= ENTROPY_THRESHOLD
    ) {
      return full.replace(v, "[REDACTED:HIGH_ENTROPY]");
    }
    return full;
  });
  return out;
}

// Redact bare identity-file references in outgoing text (e.g. a model echoing
// "see SOUL.md") so the persona/guardrail filenames aren't leaked to users.
function redactIdentityRefs(text) {
  if (!text || typeof text !== "string") return text;
  let out = text;
  for (const name of IDENTITY_FILE_BASENAMES) {
    out = out.replace(
      new RegExp(`\\b${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "gi"),
      "[REDACTED:IDENTITY_FILE]",
    );
  }
  return out;
}

function mentionsIdentityFile(text) {
  if (!text || typeof text !== "string") return false;
  const lower = text.toLowerCase();
  for (const name of IDENTITY_FILE_BASENAMES)
    if (lower.includes(name)) return true;
  return false;
}

// ---------------------------------------------------------------------------
// Prompt-injection guard — screen user/tool input for jailbreak/override.
// ---------------------------------------------------------------------------

const PROMPT_RULES = [
  {
    id: "pi-system-override",
    sev: "critical",
    why: "system-level instruction injection",
    rx: /(?:\bsystem\s*:\s*you\s+are|<<\s*SYS\s*>>|<\|im_start\|>\s*system)/i,
  },
  {
    id: "pi-ignore",
    sev: "critical",
    why: "override previous instructions",
    rx: /\b(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|rules?|prompts?|constraints?)\b/i,
  },
  {
    id: "pi-role-switch",
    sev: "high",
    why: "AI role switch attempt",
    rx: /\b(?:you\s+are\s+now|from\s+now\s+on\s+you\s+are|act\s+as\s+if\s+you\s+are|pretend\s+(?:to\s+be|you\s+are))\b/i,
  },
  {
    id: "pi-forget",
    sev: "high",
    why: "make the AI forget instructions",
    rx: /\b(?:forget\s+(?:all\s+)?(?:your|the|previous)\s+(?:instructions?|rules?|training|guidelines)|reset\s+(?:your\s+)?(?:instructions?|prompt|context))\b/i,
  },
  {
    id: "pi-dan",
    sev: "critical",
    why: "DAN jailbreak attempt",
    rx: /\b(?:DAN\s+(?:mode|prompt)|Do\s+Anything\s+Now|you\s+are\s+DAN)\b/i,
  },
  {
    id: "pi-devmode",
    sev: "high",
    why: "developer/debug mode jailbreak",
    rx: /\b(?:(?:enter|enable|activate)\s+(?:developer|debug|god|admin)\s+mode)\b/i,
  },
  {
    id: "pi-extract",
    sev: "high",
    why: "system-prompt extraction attempt",
    rx: /\b(?:(?:show|reveal|display|print|output|repeat|recite)\s+(?:me\s+)?(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|rules?))\b/i,
  },
  {
    id: "pi-unicode",
    sev: "medium",
    why: "invisible-unicode smuggling",
    rx: /[\u200B-\u200F\u2028-\u202F\uFEFF]{3,}/,
  },
];

function checkPrompt(text) {
  if (!text || typeof text !== "string") return null;
  for (const rule of PROMPT_RULES) if (rule.rx.test(text)) return rule;
  return null;
}

// Sensitive-path patterns for deferred-exec payloads.
const DEFERRED_PATH_RULES = [
  {
    id: "deferred-oc-config",
    sev: "critical",
    why: "deferred task references agent config",
    rx: /\.openclaw\/[\w.-]*\.json/i,
  },
  {
    id: "deferred-oc-state",
    sev: "critical",
    why: "deferred task references agent state dir",
    rx: /\.openclaw\/(credentials|exec-approvals|sessions|auth)/i,
  },
  {
    id: "deferred-ssh",
    sev: "critical",
    why: "deferred task references SSH keys",
    rx: /\.ssh\/(id_|authorized_keys|config\b)/i,
  },
  {
    id: "deferred-aws",
    sev: "critical",
    why: "deferred task references AWS credentials",
    rx: /\.aws\/(credentials|config\b)/i,
  },
];

function checkDeferredPayload(payload) {
  for (const rule of DEFERRED_PATH_RULES)
    if (rule.rx.test(payload)) return rule;
  return null;
}

// ---------------------------------------------------------------------------
// Skill-content scan — detect injected instructions inside a SKILL.md the
// agent reads (after_tool_call gives us the result content).
// ---------------------------------------------------------------------------

const SKILL_CONTENT_RULES = [
  {
    id: "skill-hidden-comment",
    sev: "critical",
    why: "hidden instruction in HTML comment",
    rx: /<!--[\s\S]*?\b(silently|secretly|covertly|do\s+not\s+(?:tell|mention|reveal|show))\b[\s\S]*?-->/i,
  },
  {
    id: "skill-exec-comment",
    sev: "critical",
    why: "hidden execution instruction in comment",
    rx: /<!--[\s\S]*?\b(exec|execute|run|bash|shell|curl|wget|fetch)\s*\([\s\S]*?-->/i,
  },
  {
    id: "skill-override",
    sev: "critical",
    why: "skill overrides system instructions",
    rx: /\b(?:ignore|disregard|override|forget|bypass)\s+(?:all\s+)?(?:previous|above|prior|system|safety)\s+(?:instructions?|prompts?|rules?|guidelines?|constraints?)\b/i,
  },
  {
    id: "skill-role-hijack",
    sev: "critical",
    why: "skill redefines the AI identity",
    rx: /\b(?:you\s+are\s+now|from\s+now\s+on\s+you\s+are|your\s+new\s+(?:role|identity|name)\s+is)\b/i,
  },
  {
    id: "skill-exfil",
    sev: "high",
    why: "skill data-exfiltration instruction",
    rx: /\b(?:send|post|upload|exfiltrate|transmit)\s+(?:the\s+)?(?:content|data|files?|keys?|secrets?|tokens?|credentials?)\s+(?:to|via)\s+(?:https?:\/\/|wss?:\/\/)/i,
  },
  {
    id: "skill-invisible",
    sev: "high",
    why: "skill contains invisible unicode",
    rx: /[\u200B-\u200F\u2028-\u202F\uFEFF\u00AD]{3,}/,
  },
];

function scanSkillContent(content) {
  if (!content || typeof content !== "string") return [];
  const findings = [];
  for (const rule of SKILL_CONTENT_RULES)
    if (rule.rx.test(content)) findings.push(rule);
  return findings;
}

function isSkillFile(filePath) {
  return /SKILL\.md$/i.test(String(filePath));
}

function extractResultContent(result) {
  if (!result) return null;
  if (typeof result === "string") return result;
  if (typeof result === "object") {
    for (const key of ["content", "text", "data", "output", "result"]) {
      const v = result[key];
      if (typeof v === "string" && v.length > 0) return v;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Behaviour monitor — sliding-window anomaly detection across tool calls.
// ---------------------------------------------------------------------------

const BEHAVIOR_WINDOW_MS = 60_000;
const MAX_EXEC_PER_WINDOW = 30;
const MAX_WRITES_PER_WINDOW = 50;
const CRED_SCAN_THRESHOLD = 3;
const CRED_PATH_RE =
  /\.(ssh|gnupg|aws|kube|docker)|\.env\b|credentials|\.git-credentials|shadow|sudoers/i;

const behaviorEvents = [];

function pruneBehavior() {
  const cutoff = Date.now() - BEHAVIOR_WINDOW_MS;
  while (behaviorEvents.length > 0 && behaviorEvents[0].ts < cutoff)
    behaviorEvents.shift();
}

function isCredRead(ev) {
  if (!READ_TOOLS.has(ev.toolName)) return false;
  for (const key of ["path", "file_path", "filepath", "filename", "file"]) {
    const v = ev.params?.[key];
    if (typeof v === "string" && CRED_PATH_RE.test(v)) return true;
  }
  return false;
}

function isNetworkCall(ev) {
  if (NETWORK_TOOLS.has(ev.toolName)) return true;
  if (EXEC_TOOLS.has(ev.toolName)) {
    const cmd = String(ev.params?.command ?? ev.params?.cmd ?? "");
    if (/\b(curl|wget|nc|ncat|fetch)\b/i.test(cmd)) return true;
  }
  return false;
}

function recordBehavior(ev) {
  behaviorEvents.push(ev);
  pruneBehavior();

  const execCount = behaviorEvents.filter((e) =>
    EXEC_TOOLS.has(e.toolName),
  ).length;
  if (execCount > MAX_EXEC_PER_WINDOW)
    return {
      pattern: "rapid-exec",
      sev: "high",
      why: `${execCount} exec calls in 60s (threshold ${MAX_EXEC_PER_WINDOW})`,
    };

  if (isNetworkCall(ev)) {
    const credReads = behaviorEvents.filter(isCredRead).length;
    if (credReads > 0)
      return {
        pattern: "read-then-exfil",
        sev: "critical",
        why: "credential read followed by outbound network call",
      };
  }

  const credReads = behaviorEvents.filter(isCredRead).length;
  if (credReads >= CRED_SCAN_THRESHOLD)
    return {
      pattern: "credential-scan",
      sev: "critical",
      why: `${credReads} credential-path reads in 60s (threshold ${CRED_SCAN_THRESHOLD})`,
    };

  const writeCount = behaviorEvents.filter((e) =>
    WRITE_TOOLS.has(e.toolName),
  ).length;
  if (writeCount > MAX_WRITES_PER_WINDOW)
    return {
      pattern: "mass-write",
      sev: "medium",
      why: `${writeCount} writes in 60s (threshold ${MAX_WRITES_PER_WINDOW})`,
    };

  return null;
}

// ---------------------------------------------------------------------------
// Hook wiring
// ---------------------------------------------------------------------------

function blockResult(guard, hit, tool, field, sample, ctx) {
  audit({
    decision: "deny",
    guard,
    rule: hit.id ?? hit.pattern,
    why: hit.why,
    severity: hit.sev,
    tool,
    field,
    sample: truncate(sample),
    agentId: ctx?.agentId ?? null,
    sessionKey: ctx?.sessionKey ?? null,
  });
  return {
    block: true,
    blockReason:
      `🛡️ Sentinel Guard denied: ${hit.why} ` +
      `[guard=${guard}, rule=${hit.id ?? hit.pattern}, severity=${hit.sev}]. ` +
      `Blocked by code (golden-image guardrail), not policy text.`,
  };
}

// 2026-07-09: 去 definePluginEntry 包装(openclaw 2.13/2.26 无此导出);loader
// 期望 default export 直接是 { id, register(api){...} } 对象。见同批 acl-guard 注释。
export default {
  id: "sentinel-guard",
  name: "Sentinel Guard",
  description:
    "Comprehensive code-enforced runtime security: command/path/SSRF veto, " +
    "identity-file protection, three-layer secret redaction, prompt-injection " +
    "screening, skill-content scan, and behaviour-anomaly monitoring.",
  register(api) {
    // ── before_tool_call: hard veto on dangerous calls ──────────────
    api.on(
      "before_tool_call",
      async (event, ctx) => {
        try {
          const toolName = event?.toolName ?? "";
          const params = event?.params ?? {};

          // ① Exec tools — command rules + SSRF-in-curl + skill-install
          if (EXEC_TOOLS.has(toolName)) {
            const command = extractCommand(params);
            if (command) {
              const cmdHit = checkCommand(command);
              if (cmdHit)
                return blockResult(
                  "command",
                  cmdHit,
                  toolName,
                  "command",
                  command,
                  ctx,
                );
              const urlHit = checkCommandUrls(command);
              if (urlHit)
                return blockResult(
                  "network",
                  urlHit,
                  toolName,
                  "command",
                  command,
                  ctx,
                );
            }
          }

          // ② File tools — protected-path + identity-file
          if (FILE_TOOLS.has(toolName)) {
            const fp = extractFilePath(params);
            const op = fileOperation(toolName);
            if (fp && op) {
              const hit = checkPath(fp, op);
              if (hit)
                return blockResult("path", hit, toolName, "path", fp, ctx);
            }
          }

          // ③ Message tool — refuse sending protected files as attachments
          if (MESSAGE_TOOLS.has(toolName)) {
            for (const fp of extractMessageFilePaths(params)) {
              const hit = checkPath(fp, "read");
              if (hit) {
                hit.why = `outbound attachment of protected file — ${hit.why}`;
                return blockResult(
                  "path",
                  hit,
                  toolName,
                  "attachment",
                  fp,
                  ctx,
                );
              }
            }
          }

          // ④ Dedicated network tools — SSRF on url params
          if (NETWORK_TOOLS.has(toolName)) {
            const hit = checkNetworkParams(params);
            if (hit)
              return blockResult(
                "network",
                hit,
                toolName,
                "url",
                JSON.stringify(params).slice(0, 240),
                ctx,
              );
          }

          // ⑤ Deferred-exec tools — pre-screen the scheduled payload
          if (DEFERRED_EXEC_TOOLS.has(toolName)) {
            const payload = extractDeferredPayload(toolName, params);
            if (payload) {
              const hit = checkDeferredPayload(payload);
              if (hit)
                return blockResult(
                  "deferred",
                  hit,
                  toolName,
                  "payload",
                  payload,
                  ctx,
                );
            }
          }
        } catch (_e) {
          // A guard bug must never crash the gateway. We allow the call through
          // but log the failure so it's visible. (acl-guard remains as the
          // independent secret/IMDS backstop at priority 1000.)
          audit({
            decision: "guard-error",
            hook: "before_tool_call",
            error: String(_e),
          });
        }
        return undefined;
      },
      { priority: 200 },
    );

    // ── after_tool_call: behaviour monitor + skill-content scan ──────
    api.on("after_tool_call", async (event, ctx) => {
      try {
        const toolName = event?.toolName ?? "";
        const anomaly = recordBehavior({
          ts: Date.now(),
          toolName,
          params: event?.params ?? {},
        });
        if (anomaly) {
          audit({
            decision: "anomaly",
            guard: "behavior",
            rule: anomaly.pattern,
            why: anomaly.why,
            severity: anomaly.sev,
            tool: toolName,
            agentId: ctx?.agentId ?? null,
          });
        }
        if (READ_TOOLS.has(toolName)) {
          const fp = extractFilePath(event?.params ?? {});
          if (fp && isSkillFile(fp)) {
            const content = extractResultContent(event?.result);
            if (content) {
              for (const finding of scanSkillContent(content)) {
                audit({
                  decision: "skill-injection",
                  guard: "skill",
                  rule: finding.id,
                  why: finding.why,
                  severity: finding.sev,
                  file: fp,
                  agentId: ctx?.agentId ?? null,
                });
              }
            }
          }
        }
      } catch (_e) {
        audit({
          decision: "guard-error",
          hook: "after_tool_call",
          error: String(_e),
        });
      }
    });

    // ── llm_input: reject prompt injection (best-effort) ─────────────
    api.on("llm_input", async (event) => {
      try {
        const prompt = event?.prompt;
        const hit = checkPrompt(prompt);
        if (hit) {
          audit({
            decision: "deny",
            guard: "prompt",
            rule: hit.id,
            why: hit.why,
            severity: hit.sev,
            tool: "llm_input",
          });
          return {
            block: true,
            blockReason: `🛡️ Sentinel Guard rejected input: ${hit.why} [rule=${hit.id}].`,
          };
        }
      } catch (_e) {
        audit({
          decision: "guard-error",
          hook: "llm_input",
          error: String(_e),
        });
      }
      return undefined;
    });

    // ── llm_output: redact leaked secrets in-place (best-effort) ─────
    api.on("llm_output", async (event) => {
      try {
        const texts = event?.assistantTexts;
        if (Array.isArray(texts)) {
          for (let i = 0; i < texts.length; i++) {
            if (typeof texts[i] === "string" && hasSecret(texts[i])) {
              audit({
                decision: "redact",
                guard: "secret",
                tool: "llm_output",
              });
              texts[i] = redactSecrets(texts[i]);
            }
          }
        }
      } catch (_e) {
        audit({
          decision: "guard-error",
          hook: "llm_output",
          error: String(_e),
        });
      }
    });

    // ── message_sending: last-mile redaction (always on) ─────────────
    api.on("message_sending", async (event) => {
      try {
        const content = event?.content;
        if (!content || typeof content !== "string") return undefined;
        let out = content;
        let changed = false;
        if (mentionsIdentityFile(out)) {
          out = redactIdentityRefs(out);
          changed = true;
          audit({
            decision: "redact",
            guard: "identity",
            tool: "message_sending",
          });
        }
        if (hasSecret(out)) {
          out = redactSecrets(out);
          changed = true;
          audit({
            decision: "redact",
            guard: "secret",
            tool: "message_sending",
          });
        }
        if (changed) return { content: out };
      } catch (_e) {
        audit({
          decision: "guard-error",
          hook: "message_sending",
          error: String(_e),
        });
      }
      return undefined;
    });
  },
};
