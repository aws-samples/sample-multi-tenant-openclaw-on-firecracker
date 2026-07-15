// #266 — Amazon OpenSearch (VPC-only) search adapter for vm/host log indices.
// Auth mirrors the rolesmapping bootstrap Lambda (deploy/stacks/observability.py):
// basic-auth with the master user, password fetched from Secrets Manager at
// runtime — never in env/CFN. BFF runs in-VPC (#266) so the domain is reachable.
//
// Uses node:https directly (stdlib) — no @opensearch-project client dependency
// for a single _search call.

import https from "node:https";
import { SecretsManagerClient, GetSecretValueCommand } from "@aws-sdk/client-secrets-manager";

const AOS_ENDPOINT = process.env.AOS_ENDPOINT || ""; // domain endpoint, no scheme
const AOS_SECRET_ARN = process.env.AOS_SECRET_ARN || "";
const AOS_USERNAME = process.env.AOS_MASTER_USERNAME || "claw-logs-admin";

let _sm;
let _password; // cached across warm invocations (secret is stable)

async function password() {
  if (_password) return _password;
  if (!_sm) _sm = new SecretsManagerClient({ region: process.env.AWS_REGION || "us-east-1" });
  const v = await _sm.send(new GetSecretValueCommand({ SecretId: AOS_SECRET_ARN }));
  const raw = v.SecretString || "";
  try {
    _password = JSON.parse(raw).password; // {"username","password"} shape
  } catch {
    _password = raw; // fallback: bare password
  }
  return _password;
}

function httpsJson(path, auth, body) {
  const payload = JSON.stringify(body);
  const opts = {
    host: AOS_ENDPOINT,
    port: 443,
    path,
    method: "POST",
    headers: {
      Authorization: "Basic " + auth,
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(payload),
    },
    timeout: 15000,
  };
  return new Promise((resolve, reject) => {
    const req = https.request(opts, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf-8");
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try {
            resolve(JSON.parse(text));
          } catch (e) {
            reject(new Error("aos non-JSON response: " + text.slice(0, 120)));
          }
        } else {
          reject(new Error(`aos ${res.statusCode}: ${text.slice(0, 160)}`));
        }
      });
    });
    req.on("timeout", () => req.destroy(new Error("aos request timeout")));
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

export const aos = {
  async search({ index, body }) {
    if (!AOS_ENDPOINT || !AOS_SECRET_ARN) {
      throw new Error("AOS_ENDPOINT/AOS_SECRET_ARN not configured");
    }
    const pw = await password();
    const auth = Buffer.from(`${AOS_USERNAME}:${pw}`).toString("base64");
    // Missing index (nothing ingested yet) returns 404 → allow_no_indices avoids it.
    return httpsJson(`/${encodeURIComponent(index)}/_search?ignore_unavailable=true`, auth, body);
  },
};
