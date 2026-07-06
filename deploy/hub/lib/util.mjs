// lib/util.mjs — 无状态纯工具(#136 拆分)。叶子:只 import node 内建。
// safeSend 被 ws-routing 与 server.mjs(drain/inbox)共用 → 下沉到叶子(js-split 门3)。

export function safeSend(ws, obj) {
  try {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  } catch {
    /* never throw on send */
  }
}

export function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let d = "";
    let n = 0;
    req.on("data", (c) => {
      n += c.length;
      if (n > 1_000_000) {
        reject(new Error("body too large"));
        req.destroy();
        return;
      }
      d += c;
    });
    req.on("end", () => resolve(d));
    req.on("error", reject);
  });
}
