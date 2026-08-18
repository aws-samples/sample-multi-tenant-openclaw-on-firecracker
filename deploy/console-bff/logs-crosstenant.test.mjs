// Covers: tenant_id validation (no-cross-tenant: blank/injection rejected),
// CWL Insights query build, AOS query body (vm term vs host match_phrase),
// result shaping, source routing, 400/502/504 error mapping, adapter injection.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  isValidTenantId,
  buildInsightsQuery,
  buildAosQuery,
  shapeInsightsResults,
  shapeAosHits,
  handleLambdaLogs,
  handleAosLogs,
  route,
} from "./logs.mjs";

// ── no-cross-tenant: tenant id validation is the isolation gate ──────────────
describe("isValidTenantId", () => {
  it("accepts real tenant ids", () => {
    assert.equal(isValidTenantId("acme-1a2b"), true);
    assert.equal(isValidTenantId("t-9f3c"), true);
  });
  it("rejects blank / non-string", () => {
    assert.equal(isValidTenantId(""), false);
    assert.equal(isValidTenantId(undefined), false);
    assert.equal(isValidTenantId(null), false);
    assert.equal(isValidTenantId(123), false);
  });
  it("rejects injection chars (quotes/space/wildcard/newline)", () => {
    assert.equal(isValidTenantId('a" OR "1'), false);
    assert.equal(isValidTenantId("a b"), false);
    assert.equal(isValidTenantId("a*"), false);
    assert.equal(isValidTenantId("a\nb"), false);
    assert.equal(isValidTenantId("../etc"), false);
  });
});

describe("buildInsightsQuery", () => {
  it("filters on tenant_id and sorts newest-first", () => {
    const q = buildInsightsQuery("acme-1a2b");
    assert.match(q, /filter tenant_id = "acme-1a2b"/);
    assert.match(q, /sort @timestamp desc/);
    assert.match(q, /limit 200/);
  });
});

describe("buildAosQuery", () => {
  it("vm source uses exact tenant_id.keyword term", () => {
    const b = buildAosQuery("vm", "acme-1a2b", 1000, 2000);
    assert.deepEqual(b.query.bool.must[0], { term: { "tenant_id.keyword": "acme-1a2b" } });
    assert.equal(b.query.bool.filter[0].range["@timestamp"].gte, 1000);
    assert.equal(b.query.bool.filter[0].range["@timestamp"].lte, 2000);
  });
  it("host source falls back to free-text match_phrase (no structured field)", () => {
    const b = buildAosQuery("host", "acme-1a2b", 1000, 2000);
    assert.deepEqual(b.query.bool.must[0], { match_phrase: { log: "acme-1a2b" } });
  });
  // indices written before the shipper stamped @timestamp. Without unmapped_type
  // the sort raises query_shard_exception on those and the page 502s regardless of
  // what the shipper does now — keep this assertion so the flag cannot be dropped.
  it("sorts with unmapped_type so pre-@timestamp indices cannot 502 the query", () => {
    const b = buildAosQuery("vm", "acme-1a2b", 1000, 2000);
    assert.deepEqual(b.sort, [{ "@timestamp": { order: "desc", unmapped_type: "date" } }]);
  });
});

describe("shapeInsightsResults", () => {
  it("flattens [{field,value}] rows into {ts,level,message,source}", () => {
    const rows = shapeInsightsResults([
      [
        { field: "@timestamp", value: "2026-07-15 10:00:00" },
        { field: "level", value: "ERROR" },
        { field: "message", value: "launch failed" },
        { field: "@log", value: "123:/aws/lambda/openclaw-scaler" },
      ],
    ]);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].level, "ERROR");
    assert.equal(rows[0].message, "launch failed");
    assert.equal(rows[0].source, "lambda");
  });
  it("empty/nullish → []", () => {
    assert.deepEqual(shapeInsightsResults(null), []);
    assert.deepEqual(shapeInsightsResults([]), []);
  });
});

describe("shapeAosHits", () => {
  it("maps _source.log / tenant_id / ec2_instance_id", () => {
    const rows = shapeAosHits(
      { hits: { hits: [{ _source: { "@timestamp": "t", log: "fc boot fail", tenant_id: "acme-1a2b", ec2_instance_id: "i-1" } }] } },
      "vm",
    );
    assert.equal(rows[0].message, "fc boot fail");
    assert.equal(rows[0].tenant_id, "acme-1a2b");
    assert.equal(rows[0].source, "vm");
  });
  it("missing hits → []", () => {
    assert.deepEqual(shapeAosHits({}, "vm"), []);
    assert.deepEqual(shapeAosHits(null, "vm"), []);
  });
});

// ── Handlers with injected adapters ──────────────────────────────────────────
describe("handleLambdaLogs", () => {
  it("200 with shaped rows on success", async () => {
    const cwlogs = { runInsights: async () => [[{ field: "message", value: "ok" }]] };
    const r = await handleLambdaLogs("acme-1a2b", 1000, 2000, cwlogs);
    assert.equal(r.statusCode, 200);
    const body = JSON.parse(r.body);
    assert.equal(body.source, "lambda");
    assert.equal(body.rows[0].message, "ok");
  });
  it("504 on QueryTimeout", async () => {
    const cwlogs = { runInsights: async () => { const e = new Error("t"); e.name = "QueryTimeout"; throw e; } };
    const r = await handleLambdaLogs("acme-1a2b", 1000, 2000, cwlogs);
    assert.equal(r.statusCode, 504);
  });
  it("502 on other errors", async () => {
    const cwlogs = { runInsights: async () => { throw new Error("boom"); } };
    const r = await handleLambdaLogs("acme-1a2b", 1000, 2000, cwlogs);
    assert.equal(r.statusCode, 502);
  });
});

describe("handleAosLogs", () => {
  it("200 vm exact-match, approximate=false", async () => {
    const aos = { search: async () => ({ hits: { hits: [{ _source: { log: "boot" } }] } }) };
    const r = await handleAosLogs("vm", "acme-1a2b", 1000, 2000, aos);
    assert.equal(r.statusCode, 200);
    const body = JSON.parse(r.body);
    assert.equal(body.approximate, false);
    assert.equal(body.rows[0].message, "boot");
  });
  it("200 host flagged approximate=true", async () => {
    const aos = { search: async () => ({ hits: { hits: [] } }) };
    const r = await handleAosLogs("host", "acme-1a2b", 1000, 2000, aos);
    assert.equal(JSON.parse(r.body).approximate, true);
  });
  it("502 on AOS error", async () => {
    const aos = { search: async () => { throw new Error("aos down"); } };
    const r = await handleAosLogs("vm", "acme-1a2b", 1000, 2000, aos);
    assert.equal(r.statusCode, 502);
  });
});

// ── Router ───────────────────────────────────────────────────────────────────
describe("route", () => {
  const deps = {
    cwlogs: { runInsights: async () => [] },
    aos: { search: async () => ({ hits: { hits: [] } }) },
  };
  const ev = (qs) => ({ queryStringParameters: qs });

  it("non-logs path → null (caller falls through)", async () => {
    assert.equal(await route("/traces", ev({}), deps), null);
  });
  it("400 when tenant missing", async () => {
    const r = await route("/logs", ev({ source: "vm", start: "1", end: "2" }), deps);
    assert.equal(r.statusCode, 400);
  });
  it("400 when tenant is injection", async () => {
    const r = await route("/logs", ev({ tenant: 'x" OR "1', source: "vm", start: "1", end: "2" }), deps);
    assert.equal(r.statusCode, 400);
  });
  it("400 on bad source", async () => {
    const r = await route("/logs", ev({ tenant: "acme-1", source: "bogus", start: "1", end: "2" }), deps);
    assert.equal(r.statusCode, 400);
  });
  it("400 when end<=start", async () => {
    const r = await route("/logs", ev({ tenant: "acme-1", source: "vm", start: "9", end: "2" }), deps);
    assert.equal(r.statusCode, 400);
  });
  it("routes source=lambda to CloudWatch", async () => {
    let hit = false;
    const d = { ...deps, cwlogs: { runInsights: async () => { hit = true; return []; } } };
    const r = await route("/logs", ev({ tenant: "acme-1", source: "lambda", start: "1", end: "2" }), d);
    assert.equal(r.statusCode, 200);
    assert.equal(hit, true);
  });
  it("routes source=vm to AOS", async () => {
    let hit = false;
    const d = { ...deps, aos: { search: async () => { hit = true; return { hits: { hits: [] } }; } } };
    const r = await route("/logs", ev({ tenant: "acme-1", source: "vm", start: "1", end: "2" }), d);
    assert.equal(r.statusCode, 200);
    assert.equal(hit, true);
  });
  it("defaults source to vm when omitted", async () => {
    const r = await route("/logs", ev({ tenant: "acme-1", start: "1", end: "2" }), deps);
    assert.equal(r.statusCode, 200);
    assert.equal(JSON.parse(r.body).source, "vm");
  });
});
