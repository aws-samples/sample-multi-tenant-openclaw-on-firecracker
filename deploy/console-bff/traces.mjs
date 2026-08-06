// #221 — Console BFF trace viewer routes. Read-only wrapper on X-Ray query APIs.
// Pure business logic (chunking, filter build, span tree parse) lives here so
// tests can inject a fake xray adapter without pulling the real SDK.
//
// Three endpoints (see engineering/00-knowledge-base/SPEC/kiro/platform-observability/requirements.md R6):
//   GET /capi/traces           — GetTraceSummaries  (list + filter + paging)
//   GET /capi/traces/detail    — BatchGetTraces     (≤5 ids/call, span tree)
//   GET /capi/traces/map       — GetServiceGraph    (adjacency, pass-through)
//
// The X-Ray BatchGetTraces API caps at 5 TraceIds per call — F6 in design.md.
// Any Throttled/LimitExceeded surfaces as HTTP 429; a hard xray outage → 503.

const BATCH_LIMIT = 5;

// FilterExpression: only add clauses the operator supplied; blank filter is
// valid (returns everything in the window). Values quoted with double-quotes,
// backslash-escaped so tenant_id="a\"b" would not break parsing. X-Ray's
// filter language uses the same rules as the console filter box.
export function buildFilterExpression({ tenant, errorOnly }) {
  const parts = [];
  if (tenant) {
    const safe = String(tenant).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    parts.push(`annotation.tenant_id = "${safe}"`);
  }
  if (errorOnly) {
    // R6.1: filter by error/fault. Either boolean matches an error trace.
    parts.push("(error = true OR fault = true)");
  }
  return parts.join(" AND ");
}

// Chunk trace ids into ≤5-sized batches. Empty/nullish returns [].
export function chunkTraceIds(ids, size = BATCH_LIMIT) {
  if (!Array.isArray(ids) || ids.length === 0) return [];
  const out = [];
  for (let i = 0; i < ids.length; i += size) out.push(ids.slice(i, i + size));
  return out;
}

// Parse one X-Ray Trace: segments carry a Document JSON string. Build a
// {segmentId → node} map, attach subsegments recursively; return the tree
// rooted at segments without parent_id. LimitExceeded=true is preserved on
// the wrapper so the UI can show a truncation badge.
export function parseTraceToSpanTree(trace) {
  const segments = Array.isArray(trace?.Segments) ? trace.Segments : [];
  const nodes = [];
  for (const seg of segments) {
    let doc;
    try {
      doc = typeof seg.Document === "string" ? JSON.parse(seg.Document) : seg.Document;
    } catch {
      // fail-loud but not fatal for one bad segment: skip it, keep the rest.
      continue;
    }
    if (doc) nodes.push(doc);
  }
  const byId = new Map();
  for (const n of nodes) if (n && n.id) byId.set(n.id, { ...n, subsegments: [] });

  const roots = [];
  for (const n of nodes) {
    if (!n || !n.id) continue;
    const self = byId.get(n.id);
    // Attach subsegments recursively (they arrive inline on the doc, not as
    // separate top-level entries — the segment tree is nested already).
    self.subsegments = normalizeSubsegments(n.subsegments);
    if (n.parent_id && byId.has(n.parent_id)) {
      byId.get(n.parent_id).subsegments.push(self);
    } else {
      roots.push(self);
    }
  }
  return {
    Id: trace?.Id || "",
    Duration: trace?.Duration || 0,
    LimitExceeded: trace?.LimitExceeded === true,
    Segments: roots,
  };
}

function normalizeSubsegments(subs) {
  if (!Array.isArray(subs)) return [];
  return subs.map((s) => ({ ...s, subsegments: normalizeSubsegments(s?.subsegments) }));
}

// Retryable throttling error codes (SDK v3 surfaces name = "ThrottlingException"
// but the older X-Ray-specific name is "ThrottledException"; treat both).
function isThrottled(err) {
  const name = err?.name || "";
  const status = err?.$metadata?.httpStatusCode;
  return (
    name === "ThrottledException" ||
    name === "ThrottlingException" ||
    name === "TooManyRequestsException" ||
    status === 429
  );
}

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
    { 200: "200 OK", 400: "400 Bad Request", 429: "429 Too Many Requests", 503: "503 Service Unavailable" }[code] ||
    `${code} Status`
  );
}

// ── Handlers (async, thin) ───────────────────────────────────────────────────

export async function handleList(qs, xray) {
  const start = parseUnix(qs?.start);
  const end = parseUnix(qs?.end);
  if (!start || !end || end <= start) {
    return jsonResp(400, { error: "start/end (unix seconds) required, end>start" });
  }
  const params = {
    StartTime: new Date(start * 1000),
    EndTime: new Date(end * 1000),
    TimeRangeType: "TraceId",
  };
  const filter = buildFilterExpression({ tenant: qs?.tenant, errorOnly: qs?.error === "1" || qs?.error === "true" });
  if (filter) params.FilterExpression = filter;
  if (qs?.next) params.NextToken = qs.next;
  try {
    const out = await xray.getTraceSummaries(params);
    return jsonResp(200, {
      TraceSummaries: out?.TraceSummaries || [],
      NextToken: out?.NextToken || null,
      ApproximateTime: out?.ApproximateTime || null,
      TracesProcessedCount: out?.TracesProcessedCount || 0,
    });
  } catch (e) {
    if (isThrottled(e)) return jsonResp(429, { error: "xray throttled", retryable: true });
    return jsonResp(503, { error: "xray unavailable", detail: safeMsg(e) });
  }
}

export async function handleDetail(qs, xray) {
  const raw = String(qs?.ids || "").trim();
  if (!raw) return jsonResp(400, { error: "ids= (comma-separated trace ids) required" });
  const ids = raw.split(",").map((s) => s.trim()).filter(Boolean);
  if (ids.length === 0) return jsonResp(400, { error: "no valid ids parsed" });

  const batches = chunkTraceIds(ids, BATCH_LIMIT);
  const traces = [];
  const unprocessed = [];
  let anyThrottled = false;
  for (const batch of batches) {
    try {
      const out = await xray.batchGetTraces({ TraceIds: batch });
      for (const t of out?.Traces || []) traces.push(parseTraceToSpanTree(t));
      if (Array.isArray(out?.UnprocessedTraceIds)) unprocessed.push(...out.UnprocessedTraceIds);
    } catch (e) {
      if (isThrottled(e)) {
        anyThrottled = true;
        // Mark the whole batch as unprocessed so the UI can retry just those.
        unprocessed.push(...batch);
        continue;
      }
      return jsonResp(503, { error: "xray unavailable", detail: safeMsg(e) });
    }
  }
  if (anyThrottled && traces.length === 0) {
    return jsonResp(429, { error: "xray throttled", retryable: true, UnprocessedTraceIds: unprocessed });
  }
  return jsonResp(200, { Traces: traces, UnprocessedTraceIds: unprocessed });
}

export async function handleMap(qs, xray) {
  const start = parseUnix(qs?.start);
  const end = parseUnix(qs?.end);
  if (!start || !end || end <= start) {
    return jsonResp(400, { error: "start/end (unix seconds) required, end>start" });
  }
  try {
    const out = await xray.getServiceGraph({
      StartTime: new Date(start * 1000),
      EndTime: new Date(end * 1000),
    });
    return jsonResp(200, {
      Services: out?.Services || [],
      ContainsOldGroupVersions: out?.ContainsOldGroupVersions || false,
    });
  } catch (e) {
    if (isThrottled(e)) return jsonResp(429, { error: "xray throttled", retryable: true });
    return jsonResp(503, { error: "xray unavailable", detail: safeMsg(e) });
  }
}

// ── Router — matches BFF handler style (subPath already stripped of /capi) ──

// Accepts sub-paths /traces, /traces/detail, /traces/map (with or without
// leading slash to match handler.mjs's slicing).
export async function route(subPath, event, xray) {
  const p = subPath.replace(/^\/+/, "").replace(/\/+$/, "");
  const qs = event?.queryStringParameters || {};
  if (p === "traces") return handleList(qs, xray);
  if (p === "traces/detail") return handleDetail(qs, xray);
  if (p === "traces/map") return handleMap(qs, xray);
  return null; // caller falls back to control-plane proxy
}

function parseUnix(v) {
  if (!v) return 0;
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return Math.floor(n);
}

function safeMsg(e) {
  return String(e?.message || e).slice(0, 200);
}
