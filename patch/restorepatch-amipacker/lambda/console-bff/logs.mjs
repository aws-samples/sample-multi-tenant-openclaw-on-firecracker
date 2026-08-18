//   source=lambda        → CloudWatch Logs Insights over /aws/lambda/openclaw-*
//   source=vm | host     → Amazon OpenSearch (VPC-only) claw-logs-vm / claw-logs-host
//
// Core scenario: a tenant goes unhealthy between create and running — the
// operator needs the firecracker start logs (claw-logs-vm = per-VM fc.log,
// carries tenant_id) plus the launch diagnostics (claw-logs-host) and the
// control-plane Lambda trail (CloudWatch, filtered by tenant_id).
//
// no-cross-tenant: every query is pinned to one tenant_id. A blank tenant is a
// 400 — we never return an unfiltered log dump across tenants.
//
// Pure query-build + result-shape logic lives here so tests inject fake
// cwlogs/aos adapters without pulling any SDK.

const LAMBDA_LOG_GROUP_PREFIX = "/aws/lambda/openclaw-";
const AOS_INDEX = { vm: "claw-logs-vm-*", host: "claw-logs-host-*" };
const MAX_HITS = 200;

function jsonResp(status, obj) {
  return {
    statusCode: status,
    statusDescription: reasonFor(status),
    isBase64Encoded: false,
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(obj),
  };
}

function reasonFor(code) {
  return (
    { 200: "200 OK", 400: "400 Bad Request", 502: "502 Bad Gateway", 504: "504 Gateway Timeout" }[code] ||
    `${code} Status`
  );
}

// tenant_id shape mirrors deploy/lambda/api/core/utils.py: <name>-<hash> or
// t-<hash>. Reject anything with quotes/spaces/wildcards so it can't break out
// of the CWL Insights filter string or the AOS term query.
const TENANT_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
export function isValidTenantId(t) {
  return typeof t === "string" && TENANT_RE.test(t);
}

// ── CloudWatch Logs Insights query for control-plane Lambda logs ────────────
// Powertools logs are JSON with a tenant_id key (core/logging.py:58). Filter on
// it; sort newest-first; keep the fields an operator reads while triaging a
// failed create.
export function buildInsightsQuery(tenantId) {
  return [
    "fields @timestamp, @log, level, message, tenant_id, request_id, @message",
    `filter tenant_id = "${tenantId}"`,
    "sort @timestamp desc",
    `limit ${MAX_HITS}`,
  ].join(" | ");
}

// ── AOS query body for vm/host indices ──────────────────────────────────────
// claw-logs-vm carries tenant_id (extract_tenant_id.lua). claw-logs-host does
// NOT carry a structured tenant field, so host search matches tenant_id in the
// free-text message as a fallback — best-effort, flagged to the UI.
export function buildAosQuery(source, tenantId, startMs, endMs) {
  const range = { range: { "@timestamp": { gte: startMs, lte: endMs, format: "epoch_millis" } } };
  const tenantClause =
    source === "vm"
      ? { term: { "tenant_id.keyword": tenantId } }
      : { match_phrase: { log: tenantId } };
  return {
    size: MAX_HITS,
    // AOS_INDEX is a wildcard over daily indices, so this query also hits every
    // those have no mapping for it at all. Sorting on an unmapped field is a hard
    // error (query_shard_exception → the BFF answers 502), so the whole page stays
    // broken until the last unstamped index ages out of ISM retention. Declaring
    // the type makes those indices sort as empty instead of throwing; a range on
    // an unmapped field already matches nothing, so no extra guard is needed there.
    sort: [{ "@timestamp": { order: "desc", unmapped_type: "date" } }],
    query: { bool: { filter: [range], must: [tenantClause] } },
  };
}

// Flatten CWL Insights rows ([{field,value}]) into {ts, level, message, source}.
export function shapeInsightsResults(results) {
  const rows = Array.isArray(results) ? results : [];
  return rows.map((r) => {
    const m = {};
    for (const { field, value } of r) m[field] = value;
    return {
      ts: m["@timestamp"] || "",
      level: m.level || "",
      message: m.message || m["@message"] || "",
      log_group: m["@log"] || "",
      source: "lambda",
    };
  });
}

// Flatten AOS hits into the same {ts, level, message, source} shape.
export function shapeAosHits(body, source) {
  const hits = body?.hits?.hits || [];
  return hits.map((h) => {
    const s = h._source || {};
    return {
      ts: s["@timestamp"] || "",
      level: s.level || s.PRIORITY || "",
      message: s.log || s.message || s.MESSAGE || JSON.stringify(s),
      tenant_id: s.tenant_id || "",
      ec2_instance_id: s.ec2_instance_id || "",
      source,
    };
  });
}

function parseUnixMs(v) {
  if (!v) return 0;
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return Math.floor(n) * 1000; // query params are unix seconds
}

// ── Handlers ────────────────────────────────────────────────────────────────

export async function handleLambdaLogs(tenantId, startMs, endMs, cwlogs) {
  const queryString = buildInsightsQuery(tenantId);
  try {
    const rows = await cwlogs.runInsights({
      logGroupPrefix: LAMBDA_LOG_GROUP_PREFIX,
      queryString,
      startMs,
      endMs,
    });
    return jsonResp(200, { source: "lambda", rows: shapeInsightsResults(rows) });
  } catch (e) {
    if (e?.name === "QueryTimeout") return jsonResp(504, { error: "logs insights query timed out", retryable: true });
    return jsonResp(502, { error: "cloudwatch logs unavailable", detail: safeMsg(e) });
  }
}

export async function handleAosLogs(source, tenantId, startMs, endMs, aos) {
  const index = AOS_INDEX[source];
  const body = buildAosQuery(source, tenantId, startMs, endMs);
  try {
    const res = await aos.search({ index, body });
    const rows = shapeAosHits(res, source);
    return jsonResp(200, {
      source,
      rows,
      // host index has no structured tenant field — tell the UI its match is
      // free-text, not an exact tenant pin.
      approximate: source === "host",
    });
  } catch (e) {
    return jsonResp(502, { error: "opensearch unavailable", detail: safeMsg(e) });
  }
}

// Router — subPath already stripped of /capi. GET /capi/logs?tenant&source&start&end
export async function route(subPath, event, deps) {
  const p = subPath.replace(/^\/+/, "").replace(/\/+$/, "");
  if (p !== "logs") return null;
  const qs = event?.queryStringParameters || {};
  const tenantId = qs.tenant;
  if (!isValidTenantId(tenantId)) {
    return jsonResp(400, { error: "tenant= (tenant_id) required; must match [a-z0-9-]" });
  }
  const source = qs.source || "vm";
  if (source !== "vm" && source !== "host" && source !== "lambda") {
    return jsonResp(400, { error: "source= must be vm | host | lambda" });
  }
  const startMs = parseUnixMs(qs.start);
  const endMs = parseUnixMs(qs.end);
  if (!startMs || !endMs || endMs <= startMs) {
    return jsonResp(400, { error: "start/end (unix seconds) required, end>start" });
  }
  if (source === "lambda") return handleLambdaLogs(tenantId, startMs, endMs, deps.cwlogs);
  return handleAosLogs(source, tenantId, startMs, endMs, deps.aos);
}

function safeMsg(e) {
  return String(e?.message || e).slice(0, 200);
}
