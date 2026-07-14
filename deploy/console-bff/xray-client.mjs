// Thin adapter over @aws-sdk/client-xray. Lambda Node.js 20 runtime ships the
// v3 SDK — no bundling needed. Adaptive retry mode + jitter (SDK default in v3)
// covers R6.4's backoff requirement; anything the SDK still surfaces as
// Throttling bubbles to the caller and traces.mjs maps it to HTTP 429.

import {
  XRayClient,
  GetTraceSummariesCommand,
  BatchGetTracesCommand,
  GetServiceGraphCommand,
} from "@aws-sdk/client-xray";

const client = new XRayClient({
  region: process.env.AWS_REGION || "us-east-1",
  maxAttempts: 5,
  retryMode: "adaptive",
});

export const xray = {
  getTraceSummaries: (params) => client.send(new GetTraceSummariesCommand(params)),
  batchGetTraces: (params) => client.send(new BatchGetTracesCommand(params)),
  getServiceGraph: (params) => client.send(new GetServiceGraphCommand(params)),
};
