// #266 — CloudWatch Logs Insights adapter for the per-tenant Lambda log view.
// Node 20 runtime ships @aws-sdk/client-cloudwatch-logs; no bundling. Insights
// is async (StartQuery → poll GetQueryResults), so this wraps the poll loop and
// resolves log group names from a prefix (StartQuery needs explicit names).

import {
  CloudWatchLogsClient,
  StartQueryCommand,
  GetQueryResultsCommand,
  DescribeLogGroupsCommand,
} from "@aws-sdk/client-cloudwatch-logs";

const client = new CloudWatchLogsClient({
  region: process.env.AWS_REGION || "us-east-1",
  maxAttempts: 4,
});

const POLL_INTERVAL_MS = 700;
const POLL_MAX = 20; // ~14s ceiling before we surface a QueryTimeout

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function resolveLogGroups(prefix) {
  const names = [];
  let nextToken;
  do {
    const out = await client.send(
      new DescribeLogGroupsCommand({ logGroupNamePrefix: prefix, nextToken }),
    );
    for (const g of out.logGroups || []) if (g.logGroupName) names.push(g.logGroupName);
    nextToken = out.nextToken;
  } while (nextToken);
  return names;
}

export const cwlogs = {
  // Runs an Insights query and returns the raw results array ([[{field,value}]]).
  async runInsights({ logGroupPrefix, queryString, startMs, endMs }) {
    const logGroupNames = await resolveLogGroups(logGroupPrefix);
    if (logGroupNames.length === 0) return [];
    const { queryId } = await client.send(
      new StartQueryCommand({
        logGroupNames,
        queryString,
        // StartQuery takes epoch SECONDS, not millis (API contract).
        startTime: Math.floor(startMs / 1000),
        endTime: Math.floor(endMs / 1000),
      }),
    );
    for (let i = 0; i < POLL_MAX; i++) {
      const res = await client.send(new GetQueryResultsCommand({ queryId }));
      const status = res.status;
      if (status === "Complete") return res.results || [];
      if (status === "Failed" || status === "Cancelled" || status === "Timeout") {
        const err = new Error(`insights query ${status}`);
        err.name = status === "Timeout" ? "QueryTimeout" : "QueryFailed";
        throw err;
      }
      await sleep(POLL_INTERVAL_MS);
    }
    const timeout = new Error("insights query poll ceiling reached");
    timeout.name = "QueryTimeout";
    throw timeout;
  },
};
