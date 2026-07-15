// node --test traces.test.mjs — #221 Console BFF trace viewer.
// Covers: chunk (7 ids → 2 batches), FilterExpression build, span-tree parse
// (LimitExceeded pass-through + nested subsegments), 429 throttled path,
// 503 unavailable path, NextToken pass-through.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  buildFilterExpression,
  chunkTraceIds,
  parseTraceToSpanTree,
  handleList,
  handleDetail,
  handleMap,
} from "./traces.mjs";

// ── Pure logic ───────────────────────────────────────────────────────────────

describe("buildFilterExpression", () => {
  it("empty when no filter supplied", () => {
    assert.equal(buildFilterExpression({}), "");
  });
  it("tenant only", () => {
    assert.equal(buildFilterExpression({ tenant: "t-1" }), 'annotation.tenant_id = "t-1"');
  });
  it("errorOnly only", () => {
    assert.equal(buildFilterExpression({ errorOnly: true }), "(error = true OR fault = true)");
  });
  it("tenant + errorOnly AND-joined", () => {
    assert.equal(
      buildFilterExpression({ tenant: "t-1", errorOnly: true }),
      'annotation.tenant_id = "t-1" AND (error = true OR fault = true)',
    );
  });
  it("escapes embedded quote and backslash in tenant", () => {
    assert.equal(buildFilterExpression({ tenant: 'a"b\\c' }), 'annotation.tenant_id = "a\\"b\\\\c"');
  });
});

describe("chunkTraceIds", () => {
  it("7 ids → 2 batches of 5 + 2", () => {
    const ids = ["a", "b", "c", "d", "e", "f", "g"];
    const batches = chunkTraceIds(ids, 5);
    assert.equal(batches.length, 2);
    assert.deepEqual(batches[0], ["a", "b", "c", "d", "e"]);
    assert.deepEqual(batches[1], ["f", "g"]);
  });
  it("empty/nullish → []", () => {
    assert.deepEqual(chunkTraceIds([]), []);
    assert.deepEqual(chunkTraceIds(null), []);
    assert.deepEqual(chunkTraceIds(undefined), []);
  });
  it("exact multiple boundary — 10 ids → 2 batches of 5", () => {
    const ids = Array.from({ length: 10 }, (_, i) => "t" + i);
    const batches = chunkTraceIds(ids, 5);
    assert.equal(batches.length, 2);
    assert.equal(batches[0].length, 5);
    assert.equal(batches[1].length, 5);
  });
});

describe("parseTraceToSpanTree", () => {
  it("nests children by parent_id and preserves LimitExceeded", () => {
    const trace = {
      Id: "1-abc",
      Duration: 0.5,
      LimitExceeded: true,
      Segments: [
        { Document: JSON.stringify({ id: "root", name: "svc-a", subsegments: [{ id: "sub1", name: "s3" }] }) },
        { Document: JSON.stringify({ id: "child", parent_id: "root", name: "svc-b" }) },
      ],
    };
    const tree = parseTraceToSpanTree(trace);
    assert.equal(tree.Id, "1-abc");
    assert.equal(tree.LimitExceeded, true);
    assert.equal(tree.Segments.length, 1);
    const root = tree.Segments[0];
    assert.equal(root.name, "svc-a");
    // inline subsegments from the document survive
    assert.equal(root.subsegments.length, 2);
    const names = root.subsegments.map((s) => s.name).sort();
    assert.deepEqual(names, ["s3", "svc-b"]);
  });
  it("skips a segment whose Document is unparseable, keeps the rest", () => {
    const tree = parseTraceToSpanTree({
      Id: "1-x",
      Segments: [{ Document: "not-json" }, { Document: JSON.stringify({ id: "ok" }) }],
    });
    assert.equal(tree.Segments.length, 1);
    assert.equal(tree.Segments[0].id, "ok");
  });
  it("LimitExceeded defaults to false when absent", () => {
    const tree = parseTraceToSpanTree({ Id: "1", Segments: [] });
    assert.equal(tree.LimitExceeded, false);
  });
});

// ── Handlers with stub xray ──────────────────────────────────────────────────

function stubXray(overrides = {}) {
  const calls = { getTraceSummaries: [], batchGetTraces: [], getServiceGraph: [] };
  return {
    calls,
    getTraceSummaries: async (p) => {
      calls.getTraceSummaries.push(p);
      if (overrides.getTraceSummaries) return overrides.getTraceSummaries(p);
      return { TraceSummaries: [], NextToken: null };
    },
    batchGetTraces: async (p) => {
      calls.batchGetTraces.push(p);
      if (overrides.batchGetTraces) return overrides.batchGetTraces(p);
      return { Traces: [] };
    },
    getServiceGraph: async (p) => {
      calls.getServiceGraph.push(p);
      if (overrides.getServiceGraph) return overrides.getServiceGraph(p);
      return { Services: [] };
    },
  };
}

const NOW = 1720000000;

describe("handleList", () => {
  it("400 when start/end missing", async () => {
    const resp = await handleList({}, stubXray());
    assert.equal(resp.statusCode, 400);
  });
  it("passes NextToken through and returns 200 with SDK output", async () => {
    const xray = stubXray({
      getTraceSummaries: async () => ({
        TraceSummaries: [{ Id: "1-a" }],
        NextToken: "next-page-token",
        TracesProcessedCount: 42,
      }),
    });
    const resp = await handleList(
      { start: String(NOW - 3600), end: String(NOW), next: "page-2", tenant: "t-1", error: "1" },
      xray,
    );
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    assert.equal(body.NextToken, "next-page-token");
    assert.equal(body.TracesProcessedCount, 42);
    // Filter + NextToken forwarded to SDK.
    const call = xray.calls.getTraceSummaries[0];
    assert.equal(call.NextToken, "page-2");
    assert.match(call.FilterExpression, /tenant_id = "t-1"/);
    assert.match(call.FilterExpression, /error = true/);
  });
  it("throttled → 429 with retryable:true", async () => {
    const xray = stubXray({
      getTraceSummaries: async () => {
        const e = new Error("Rate exceeded");
        e.name = "ThrottlingException";
        throw e;
      },
    });
    const resp = await handleList({ start: String(NOW - 60), end: String(NOW) }, xray);
    assert.equal(resp.statusCode, 429);
    assert.equal(JSON.parse(resp.body).retryable, true);
  });
  it("non-throttle error → 503 degradation path (R14.11)", async () => {
    const xray = stubXray({
      getTraceSummaries: async () => {
        throw new Error("connection refused");
      },
    });
    const resp = await handleList({ start: String(NOW - 60), end: String(NOW) }, xray);
    assert.equal(resp.statusCode, 503);
    assert.equal(JSON.parse(resp.body).error, "xray unavailable");
  });
});

describe("handleDetail", () => {
  it("7 ids → 2 SDK batches, aggregates traces and UnprocessedTraceIds", async () => {
    const xray = stubXray({
      batchGetTraces: async (p) => {
        // 1st batch returns 2 traces + 1 unprocessed, 2nd returns 1 trace.
        if (p.TraceIds.length === 5) {
          return {
            Traces: [
              { Id: "1-a", Segments: [{ Document: JSON.stringify({ id: "r1" }) }] },
              { Id: "1-b", Segments: [{ Document: JSON.stringify({ id: "r2" }) }] },
            ],
            UnprocessedTraceIds: ["1-c"],
          };
        }
        return { Traces: [{ Id: "1-f", Segments: [{ Document: JSON.stringify({ id: "r6" }) }] }] };
      },
    });
    const ids = ["1-a", "1-b", "1-c", "1-d", "1-e", "1-f", "1-g"].join(",");
    const resp = await handleDetail({ ids }, xray);
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    assert.equal(xray.calls.batchGetTraces.length, 2);
    assert.equal(xray.calls.batchGetTraces[0].TraceIds.length, 5);
    assert.equal(xray.calls.batchGetTraces[1].TraceIds.length, 2);
    assert.equal(body.Traces.length, 3);
    assert.deepEqual(body.UnprocessedTraceIds, ["1-c"]);
  });
  it("400 when ids missing", async () => {
    const resp = await handleDetail({}, stubXray());
    assert.equal(resp.statusCode, 400);
  });
  it("partial throttle: 1st batch throttled, 2nd ok → 200 with partial traces + Unprocessed", async () => {
    let n = 0;
    const xray = stubXray({
      batchGetTraces: async (p) => {
        n++;
        if (n === 1) {
          const e = new Error("throttled");
          e.name = "ThrottledException";
          throw e;
        }
        return { Traces: [{ Id: "1-ok", Segments: [] }] };
      },
    });
    const ids = ["1-a", "1-b", "1-c", "1-d", "1-e", "1-f"].join(",");
    const resp = await handleDetail({ ids }, xray);
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    assert.equal(body.Traces.length, 1);
    // First batch's 5 ids should appear in UnprocessedTraceIds.
    assert.equal(body.UnprocessedTraceIds.length, 5);
  });
  it("all batches throttled → 429", async () => {
    const xray = stubXray({
      batchGetTraces: async () => {
        const e = new Error("throttled");
        e.name = "ThrottlingException";
        throw e;
      },
    });
    const resp = await handleDetail({ ids: "1-a,1-b" }, xray);
    assert.equal(resp.statusCode, 429);
  });
});

describe("handleMap", () => {
  it("passes through Services from GetServiceGraph", async () => {
    const xray = stubXray({
      getServiceGraph: async () => ({ Services: [{ Name: "api" }, { Name: "consumer" }] }),
    });
    const resp = await handleMap({ start: String(NOW - 60), end: String(NOW) }, xray);
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    assert.equal(body.Services.length, 2);
  });
  it("throttled → 429", async () => {
    const xray = stubXray({
      getServiceGraph: async () => {
        const e = new Error("throttled");
        e.$metadata = { httpStatusCode: 429 };
        throw e;
      },
    });
    const resp = await handleMap({ start: String(NOW - 60), end: String(NOW) }, xray);
    assert.equal(resp.statusCode, 429);
  });
});
