// acl-guard — programmatic before_tool_call deny-list (L3 "answer-side" enforcement).
//
// WHY: ops-guardrails as a skill only lives in the prompt (model self-discipline).
// Red-teamers bypass that by talking the model into running the command anyway.
// This plugin moves enforcement DOWN to the tool-execution layer: OpenClaw fires
// `before_tool_call` BEFORE any tool runs, and a returned { block: true } is a
// hard veto in the runtime (kind "veto", the tool never executes). So even a
// fully jailbroken model cannot exfiltrate secrets — the deny is in code, not
// in the model's good behaviour.
//
// Contract (verified against /usr/lib/node_modules/openclaw/docs/plugins/hooks.md
// line 121: "before_tool_call — rewrite tool params, block execution, or require
// approval"): handler gets { toolName, params, ctx }, returns
// { block: true, blockReason } to veto.
//
// 2026-07-09: 去掉 `definePluginEntry` 包装(openclaw/plugin-sdk/plugin-entry)。
// 该导出在 openclaw 2026.2.13/2.26 已不存在(是早期版本 API);2.13/2.26 的
// plugins/loader.ts:resolvePluginModuleExport 期望 default export 直接是
// { id, name, register(api){...} } 对象或函数(register?/activate? 二选一,
// types.ts:236)。register 内 api.on("before_tool_call", ...) 与 hook 名在
// 2.13/2.26 完全兼容(types.ts:278/306),故只改导出形态,内部逻辑不动。
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

// Machine-readable deny-list. Each rule: a name + a matcher over the tool's
// command/path/url fields. Patterns target the exact exfil classes ops-guardrails
// describes in prose, now enforced deterministically.
const DENY_RULES = [
  {
    id: "env-dump",
    rx: /(^|[\s;&|`(])(env|printenv)([\s;&|`)]|$)/i,
    why: "environment dump (env/printenv)",
  },
  {
    id: "dotenv-read",
    rx: /(\.env\b|\/\.env|\.env\.[\w.]+)/i,
    why: "read .env file",
  },
  {
    id: "proc-environ",
    rx: /\/proc\/[^/\s]+\/environ/i,
    why: "read /proc/<pid>/environ",
  },
  {
    id: "aws-creds",
    rx: /\.aws\/(credentials|config)\b/i,
    why: "read ~/.aws credentials/config",
  },
  {
    // Command-agnostic: any reference to an .ssh dir / private key path is
    // denied regardless of the binary used. sentinel-guard's cred-ssh rule
    // only covers a command allow-list (cat/cp/...) and misses awk/dd/base64;
    // matching on the path here closes that bypass at the deny layer.
    id: "ssh-key",
    rx: /\.ssh\/|\bid_(rsa|ed25519|ecdsa|dsa)\b|authorized_keys\b/i,
    why: "read ~/.ssh private key / authorized_keys",
  },
  {
    id: "aws-env",
    rx: /\bAWS_(SECRET_ACCESS_KEY|SESSION_TOKEN|ACCESS_KEY_ID)\b/i,
    why: "AWS credential env var",
  },
  {
    id: "imds-ip",
    rx: /169\.254\.169\.25[34]/,
    why: "IMDS link-local access (169.254.169.254)",
  },
  {
    id: "imds-creds-path",
    rx: /iam\/security-credentials|instance-identity\/document/i,
    why: "IMDS credential path",
  },
  {
    id: "secret-grep",
    rx: /\b(SECRET|TOKEN|PASSWORD|API[_-]?KEY)\b/i,
    why: "secret-keyword scan",
    fieldsOnly: ["command"],
  },
];

const LOG_PATH =
  process.env.OPENCLAW_ACL_LOG || "/home/agent/.openclaw/logs/acl-deny.log";

// Pull the human-supplied strings out of whatever tool is being called.
// exec-class tools carry `command`/`cmd`; file-class carry `path`/`filePath`/
// `file`/`target`; network tools carry `url`. We inspect them all.
const FIELDS = [
  "command",
  "cmd",
  "args",
  "path",
  "filePath",
  "file",
  "target",
  "url",
  "query",
];
function harvest(params) {
  if (!params || typeof params !== "object") return [];
  const out = [];
  for (const f of FIELDS) {
    const v = params[f];
    if (v == null) continue;
    if (Array.isArray(v)) out.push([f, v.map(String).join(" ")]);
    else out.push([f, String(v)]);
  }
  return out;
}

function logDeny(entry) {
  try {
    mkdirSync(dirname(LOG_PATH), { recursive: true });
    appendFileSync(LOG_PATH, JSON.stringify(entry) + "\n");
  } catch (_e) {
    // Never let logging failure crash the hook — failing closed (still blocking)
    // is the safe outcome; we just lose the audit line.
  }
}

export default {
  id: "acl-guard",
  name: "ACL Guard",
  description:
    "Pre-tool-call deny-list for secret/IMDS exfiltration (code-enforced ops-guardrails).",
  register(api) {
    api.on(
      "before_tool_call",
      async (event) => {
        const toolName = event?.toolName ?? "";
        const harvested = harvest(event?.params);
        if (harvested.length === 0) return; // nothing inspectable -> allow

        for (const [field, value] of harvested) {
          for (const rule of DENY_RULES) {
            if (rule.fieldsOnly && !rule.fieldsOnly.includes(field)) continue;
            if (rule.rx.test(value)) {
              const sample =
                value.length > 240 ? value.slice(0, 240) + "…" : value;
              logDeny({
                ts: new Date().toISOString(),
                decision: "deny",
                rule: rule.id,
                why: rule.why,
                tool: toolName,
                field,
                sample, // truncated; do not log full secrets
                agentId: event?.ctx?.agentId ?? null,
                sessionKey: event?.ctx?.sessionKey ?? null,
              });
              return {
                block: true,
                blockReason: `ACL Guard denied: ${rule.why}. This action is blocked by code (ops-guardrails enforcement), not policy text.`,
              };
            }
          }
        }
        // no rule matched -> implicit allow (return undefined)
      },
      { priority: 1000 }, // run before any other tool hook
    );
  },
};
