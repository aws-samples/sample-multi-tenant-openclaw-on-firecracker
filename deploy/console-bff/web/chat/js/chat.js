// ClawPool chat 主逻辑。
// #63 (CSP 外链化) — 从 index.html 内联 <script> 搬出,配合 CloudFront
// ResponseHeadersPolicy 的 script-src 'self'。逻辑与原内联完全一致,
// 只在文件末尾新增 setupDelegatedHandlers() 通过事件委托绑定被删掉的
// onclick=/oninput=/onkeydown=/onchange= 内联属性(index.html 里改成
// data-action="..." 或 id)。这是搬 script 的必要配套,不改任何原有行为。

// ============ 运行环境探测 ============
// #187 转型:数据面从 claw-channel + wss hub 中枢改为**两级路由直连 microVM
// 原生 gateway** —— POST {ORIGIN}/ws/{tenant_id}/v1/chat/completions,
// CloudFront /ws/* → ALB LOR → 3 台 OpenResty edge → Redis 查 route:{tenant_id}
// 拿 host:port → 宿主 iptables DNAT → microVM:18789 OpenClaw gateway。
// 认证:Authorization: Bearer <gateway_token>,per-租户唯一,由 P1 KMS 预铸
// (the API spec 二);token 明文由 tenant owner 从 console reveal 页面
// 手动贴到 chat 页 localStorage.oc_gw_token(P5 讨论平台后端 relay 是否兜底,
// 本 chat mini-app 走"用户持 token"最简形态)。
// 现场 demo(file:// / localhost):走本地代理 127.0.0.1:8799/chat(token 在代理进程内存)。
const IS_LOCAL = !!window.OC_LOCAL;
const PROXY = "http://127.0.0.1:8799"; // 本地代理(SSH 隧道到 metal)— 仅 demo 兜底
const ORIGIN = window.location.origin; // 同源 CloudFront,反代 /vm/*

// 静态降级节点(API 拉不到时显示,保证界面不空;对话仍可尝试经反代连通)
const STATIC_NODES = [
  {
    id: "ro-test-01",
    tenant: "ro-test-01",
    label: "ro-test-01",
    host: "metal · vm47",
    online: true,
  },
];

let NODES = [];
let active = null;
let history = [];

// ============ 节点列表:从控制面 API 动态拉 ============
// GET /tenants 带 Cognito id_token(Bearer)+ x-api-key。
// RBAC owner 过滤后只返回当前用户自己的节点。拉不到就降级静态。
function idToken() {
  return localStorage.getItem("oc_id_token") || "";
}
// 当前登录用户的 Cognito sub(解 id_token)。用于在节点列表里优先选中
// **自己 owner 的**节点——admin 角色会从 GET /tenants 拿到全部节点,若盲选
// NODES[0] 可能选中别人的节点→发消息时 hub authorizeSubForTenant 判 sub≠owner
// 返回 "not authorized for this tenant"。自助节点 id 形如 u-{sub前8}-xxxx。
function mySub() {
  const t = idToken();
  if (t.split(".").length !== 3) return "";
  try {
    const p = JSON.parse(
      atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")),
    );
    return p.sub || "";
  } catch (e) {
    return "";
  }
}
// 从节点列表挑「属于当前用户」的优先项:id 以 u-{sub前8} 开头(自助命名约定)。
function pickOwnNode(nodes) {
  const sub = mySub();
  if (sub) {
    const pfx = "u-" + sub.slice(0, 8).toLowerCase();
    const own = nodes.find((n) =>
      (n.id || "").toLowerCase().startsWith(pfx),
    );
    if (own) return own;
  }
  return nodes[0];
}
function apiKey() {
  // 共享网关限流 key(非用户私密,API Gateway usage-plan key,非授权凭据)。
  // 优先用部署时注入的 window.OC_API_KEY(setup.sh 上传 chat 页时 sed 注入,
  // 不写死进 git 源码——红线);否则回退控制台共用的 localStorage.oc_api_key。
  // 授权仍由 Cognito id_token + 后端 owner scoping 决定;这个 key 只为过
  // API Gateway 的限流门,自助用户不必手动输 key 也能列出+连自己的节点。
  return window.OC_API_KEY || localStorage.getItem("oc_api_key") || "";
}

async function loadNodes(opts) {
  opts = opts || {};
  const el = document.getElementById("nodeList");
  if (IS_LOCAL) {
    NODES = STATIC_NODES.slice();
    if (!active) active = NODES[0];
    renderNodes();
    return;
  }
  try {
    const url = window.OC_API_URL.replace(/\/+$/, "") + "/tenants";
    // Cognito id_token 是授权凭据:GET /tenants 带它(非 admin)后端按
    // owner_id 只返回调用者名下的节点(handler.py issue #80 owner scoping),
    // 所以自助开通的用户**不需要** gateway key 就能列出+连自己的节点。
    // x-api-key 只是 API Gateway 的限流 key(可选);页面注入了就带上,
    // 没有也照常发(跟 provisionMyNode 一致)——缺它不该阻止用户看到自己的节点。
    const headers = { Authorization: "Bearer " + idToken() };
    const k = apiKey();
    if (k) headers["x-api-key"] = k;
    const r = await fetch(url, { headers });
    if (r.status === 401) throw new Error("NEED_LOGIN");
    if (r.status === 403 && !k) throw new Error("NO_API_KEY");
    if (r.status === 403) throw new Error("BAD_API_KEY");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const items = await r.json();
    if (!Array.isArray(items)) throw new Error("bad shape");
    // chat 是 C 端对话:只展示**当前用户自己 owner 的** running 节点。
    // 非 admin 后端已 owner-scope;但 admin 角色(运营兼用同账号)会拿到全部
    // 节点,若不在前端再过滤一次,会列出别人的节点→选中后发消息撞 hub
    // authorizeSubForTenant 的 'not authorized for this tenant'(sub≠owner)。
    // 有 sub 就按 owner_id===sub 过滤;拿不到 sub(降级)才不过滤。
    const _sub = mySub();
    const mapped = items
      .filter((t) => (t.status || "") === "running")
      .filter((t) => !_sub || !t.owner_id || t.owner_id === _sub)
      .map((t) => ({
        id: t.id,
        tenant: t.id,
        label: t.name || t.id,
        host: (t.host_id || "—") + (t.guest_ip ? " · " + t.guest_ip : ""),
        online: (t.app_health || "") === "up" || t.status === "running",
        // #15: 不再在浏览器侧持有 gateway_token(已由 #100 从 API 响应剥离,
        // 且 chat 走 hub 短时 token,gateway_token 永不进浏览器)。删死字段。
      }));
    if (mapped.length === 0) throw new Error("no running tenants");
    NODES = mapped;
    // 优先选当前用户自己 owner 的节点(admin 会看到全部节点,盲选 NODES[0]
    // 可能选中别人的→hub 返回 not authorized for this tenant)。
    if (!active || !NODES.find((n) => n.id === active.id))
      active = pickOwnNode(NODES);
    renderNodes();
    selectNode(active.id, true);
  } catch (e) {
    const msg = String(e.message || e);
    // 缺/错网关 key — 提示用户输入一次(存 localStorage,不进源码)。
    // 不自动弹窗骚扰;在节点区放一个「连接」入口,点了才提示。
    if (
      (msg === "NO_API_KEY" || msg === "BAD_API_KEY") &&
      !opts.noPrompt
    ) {
      // The gateway key is ONLY for the optional "list my nodes" sidebar;
      // chat itself runs over Cognito + the hub WS and needs no key. So a
      // missing key is not an error — render the quiet sidebar prompt but
      // do NOT raise the alarming red connection banner (it falsely reads
      // as "chat is broken"). Only a genuinely bad key warrants the banner.
      renderNeedKey(msg === "BAD_API_KEY");
      if (msg === "BAD_API_KEY") {
        showConn(
          "网关连接 key 无效(仅影响左侧节点列表,不影响对话)。",
          true,
        );
      }
      return;
    }
    // 其它失败:仅本地开发可降级到静态节点(走本地代理,不涉真实租户隔离)。
    // 生产环境绝不降级到共享静态节点——那等于把拉不到自己节点的用户路由到
    // 一个公共节点(跨租户泄漏)。生产失败时清空节点列表 + 明确提示,路由到任何
    // 节点都不发生。
    if (IS_LOCAL) {
      NODES = STATIC_NODES.slice();
      if (!active) active = NODES[0];
      renderNodes();
      showConn(
        "本地节点列表降级为静态(API 未返回:" + msg.slice(0, 60) + ")。",
        true,
      );
    } else {
      NODES = [];
      active = null;
      renderNodes();
      showConn(
        "无法加载你的节点列表(" +
          msg.slice(0, 60) +
          ")。请刷新或联系管理员开通节点。",
        true,
      );
    }
  }
}

// 缺网关 key 时的引导(点击才输入,key 只进 localStorage)
function renderNeedKey(wasBad) {
  const el = document.getElementById("nodeList");
  el.innerHTML = `
<div class="empty">
  ${wasBad ? "网关连接 key 无效。" : "尚未配置网关连接 key。"}<br>
  配置后即可列出你名下的运行中节点。<br><br>
  <span class="rf" style="border:1px solid var(--line2);border-radius:7px;padding:6px 12px;color:var(--brand);cursor:pointer"
        data-action="prompt-key">输入网关连接 key</span>
</div>`;
}
function promptKey() {
  const k = window.prompt(
    "输入网关连接 key(由管理员提供;仅存本浏览器 localStorage,不写入页面源码):",
  );
  if (k && k.trim()) {
    localStorage.setItem("oc_api_key", k.trim());
    showConn("已保存连接 key,正在拉取节点…", false);
    loadNodes();
  }
}

function renderNodes() {
  const el = document.getElementById("nodeList");
  if (!NODES.length) {
    // 自助开通:名下无节点时,让用户一键为自己开通(POST /tenants/self),
    // 不再只提示"联系管理员"。按钮在开通中禁用并显示进度。
    el.innerHTML = `
  <div class="empty">
    你名下还没有 AI 节点。<br><br>
    <span id="provisionBtn" class="rf"
          style="border:1px solid var(--line2);border-radius:7px;padding:8px 14px;color:var(--brand);cursor:pointer"
          data-action="provision-node">＋ 开通我的 AI 节点</span>
    <div id="provisionMsg" style="margin-top:10px;color:var(--muted);font-size:12px"></div>
  </div>`;
    return;
  }
  el.innerHTML = NODES.map(
    (n) => `
<div class="node ${active && n.id === active.id ? "on" : ""}" data-action="select-node" data-node-id="${escapeHtml(n.id)}" style="cursor:pointer">
  <div class="nt"><span class="dot ${n.online ? "" : "off"}"></span>${escapeHtml(n.label)}</div>
  <div class="meta">${escapeHtml(n.host)} · ${n.online ? "running" : "stopped"}</div>
</div>`,
  ).join("");
}

// 自助开通自己的 openclaw 节点:POST /tenants/self(owner 由后端绑登录态,
// 后端有 per-user 上限防滥用)→ 轮询到 running → 自动选中进对话。
let _provisioning = false;
async function provisionMyNode() {
  if (_provisioning) return;
  _provisioning = true;
  const btn = document.getElementById("provisionBtn");
  const msg = document.getElementById("provisionMsg");
  const setMsg = (t) => {
    if (msg) msg.textContent = t;
  };
  if (btn) {
    btn.style.opacity = "0.5";
    btn.style.pointerEvents = "none";
  }
  try {
    const base = window.OC_API_URL.replace(/\/+$/, "");
    const headers = {
      Authorization: "Bearer " + idToken(),
      "Content-Type": "application/json",
    };
    const k = apiKey();
    if (k) headers["x-api-key"] = k;
    setMsg("正在为你开通 AI 节点…");
    const r = await fetch(base + "/tenants/self", {
      method: "POST",
      headers,
      body: JSON.stringify({}),
    });
    if (r.status === 401) throw new Error("请先登录再开通");
    if (r.status === 409) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || "已达节点上限");
    }
    if (!r.ok) throw new Error("开通失败 HTTP " + r.status);
    const created = await r.json();
    const newId = created.id;
    setMsg("节点创建中(creating→running),约需 30 秒…");
    // 轮询 GET /tenants 直到这个新节点 running(最多 ~90s)
    for (let i = 0; i < 30; i++) {
      await new Promise((s) => setTimeout(s, 3000));
      await loadNodes({ noPrompt: true });
      const found = NODES.find((n) => n.id === newId);
      if (found) {
        setMsg("节点已就绪,正在进入对话…");
        selectNode(newId);
        _provisioning = false;
        return;
      }
    }
    setMsg("节点已创建,仍在启动中。稍后点刷新(↻)即可看到。");
  } catch (e) {
    setMsg("开通失败:" + (e && e.message ? e.message : e));
    if (btn) {
      btn.style.opacity = "1";
      btn.style.pointerEvents = "auto";
    }
  } finally {
    _provisioning = false;
  }
}

function selectNode(id, silent) {
  active = NODES.find((n) => n.id === id) || active;
  if (!active) return;
  document.getElementById("hdrNode").textContent =
    active.label + (active.online ? " · 在线" : " · 离线");
  renderNodes();
  if (!silent) resetChat();
  if (active.online) {
    showConn(
      IS_LOCAL
        ? "已连接 OpenClaw gateway · Claude Sonnet 4.6 · 经本地代理"
        : "已连接 OpenClaw gateway · Claude Sonnet 4.6 · 经 CloudFront 反代(同源)",
      false,
    );
  } else {
    showConn(`${active.label} 当前离线 — 选一个 running 节点对话`, true);
  }
}

// 多会话 threadId: one threadId per conversation. "新建对话"
// mints a fresh one so the agent gets an isolated context (channel embeds it
// as sub:t:threadId in the session key). Persisted so a page refresh keeps
// the same conversation. crypto.randomUUID with a fallback.
function newThreadId() {
  try {
    if (crypto && crypto.randomUUID) return crypto.randomUUID();
  } catch (_) {}
  return (
    "t-" +
    Date.now().toString(36) +
    "-" +
    Math.random().toString(36).slice(2, 8)
  );
}
// #41: a thread persisted in sessionStorage means this is a refresh/reopen
// of an existing conversation → auto-load its history on boot. A brand-new
// thread (fresh login) has nothing to load.
const _threadPreexisted = !!sessionStorage.getItem("oc_active_thread");
let _activeThreadId =
  sessionStorage.getItem("oc_active_thread") || newThreadId();
sessionStorage.setItem("oc_active_thread", _activeThreadId);
function resetChat() {
  history = [];
  document.getElementById("chat").innerHTML =
    document.getElementById("welcome").outerHTML;
}
function newSession() {
  // #13 先丢弃当前 thread 在途的请求(reject + 清门控),再切新 thread。
  // 否则旧 thread 的 reply_delta 会写到马上被 resetChat 移除的气泡,且发送
  // 门控卡在 true 让新会话发不出消息,直到旧请求 90s 超时。
  _abortInflight("已切换到新对话");
  // start a fresh isolated conversation thread
  _activeThreadId = newThreadId();
  sessionStorage.setItem("oc_active_thread", _activeThreadId);
  resetChat();
}

// #187 转型:会话历史回看依赖 hub WS 的 history_request/history_reply 帧,
// gateway 直连模式下 microVM 内的原生 gateway 是 stateless per-request 语义,
// 不吐历史(会话状态在 agent 的 threadId 里,但没有独立读取 API)。历史面向
// 后续独立 issue(需 agent 侧暴露 /threads/{id}/messages 或 broker 兜底)。
function resolveActiveNode() {
  return active || null;
}
async function loadHistory() {
  showConn(
    "两级路由直连模式下,会话历史暂不支持前端拉取(gateway 无独立读取 API)。",
    true,
  );
}
function quickAsk(t) {
  document.getElementById("input").value = t;
  send();
}

// 思考过程面板:按用户问题推断 agent 正在做的步骤,在回复气泡里轮换显示
// (对齐主流 AI 助手的 StatusParts 推理展示)。返回 interval id,调用方在
// 回复到达/出错时 clearInterval。纯前端 UX,文案是中性的"正在做某类事"。
function thinkingSteps(q) {
  const r = (q || "").toLowerCase();
  const has = (...ws) => ws.some((w) => r.includes(w));
  if (has("btc", "eth", "价位", "价格", "行情", "多少钱", "$"))
    return [
      "接收请求",
      "拉取平台实时行情",
      "解析价位与多空关键位",
      "组织回复",
    ];
  if (has("审计", "风险", "合约", "蜜罐", "honeypot", "安全"))
    return [
      "接收请求",
      "调用风险评估数据源",
      "检查蜜罐/owner/税率",
      "汇总风险点",
    ];
  if (has("策略", "回测", "backtest", "信号", "指标"))
    return [
      "接收请求",
      "拉历史K线",
      "计算技术指标/信号",
      "校验无未来数据",
      "组织结论",
    ];
  if (has("身份", "你是谁", "skill", "技能", "能做"))
    return [
      "接收请求",
      "读取身份与能力清单",
      "确认沙箱与不可篡改约束",
      "组织回复",
    ];
  if (has("转账", "下单", "买", "卖", "划转", "支付"))
    return [
      "接收请求",
      "识别资金动作意图",
      "进入确认门控(CONFIRM)",
      "等待你二次确认",
    ];
  return ["接收请求", "理解意图", "检索相关数据", "组织回复"];
}
function startThinking(ctEl, q) {
  const steps = thinkingSteps(q);
  let i = 0;
  const render = () => {
    const cur = steps[Math.min(i, steps.length - 1)];
    ctEl.innerHTML =
      '<div class="think"><span class="think-dot"></span>' +
      '<span class="think-tx">' +
      cur +
      "…</span></div>";
  };
  render();
  return setInterval(() => {
    i++;
    if (i < steps.length) render();
  }, 1400);
}

// Next-Actions: 每条回复后给 2-3 个相关「下一步」建议(对齐主流 AI 助手
// 的引导按钮)。按回复内容关键词派生,点击回填并发送。纯前端,不依赖后端协议。
function suggestNextActions(reply) {
  const r = (reply || "").toLowerCase();
  const has = (...ws) => ws.some((w) => r.includes(w));
  const out = [];
  if (has("btc", "eth", "价位", "价格", "行情", "$", "usdt")) {
    out.push([
      "📊 看资金费率",
      "这个币当前的合约资金费率和持仓量怎么样?",
    ]);
    out.push(["📈 技术信号", "帮我用 SMA/RSI 算下它的短期技术信号。"]);
  }
  if (has("审计", "风险", "合约", "蜜罐", "honeypot", "lp")) {
    out.push([
      "🔍 查持有人分布",
      "这个代币的持有人集中度和 LP 锁仓情况?",
    ]);
  }
  if (has("策略", "回测", "backtest", "信号")) {
    out.push([
      "🧪 跑回测",
      "用这个策略在 testnet 跑一次回测,注意别用未来数据。",
    ]);
  }
  if (has("身份", "skill", "技能", "clawpool agent", "🐋")) {
    out.push(["🛡️ 能力边界", "你哪些技能是只读不可篡改的?能动我的钱吗?"]);
  }
  // 默认兜底:总给一组通用下一步
  if (out.length === 0) {
    out.push(["📈 BTC 行情", "BTC 现在什么价位?短期多空关键位?"]);
    out.push(["🔍 代币审计", "帮我看一个代币合约的风险点。"]);
  }
  return out.slice(0, 3);
}

function renderNextActions(reply) {
  const acts = suggestNextActions(reply);
  if (!acts.length) return "";
  const chips = acts
    .map(
      (a) =>
        // #63 CSP:动态 chip 不写内联 onclick,把 payload 放 data-quick,
        // 委托监听在 setupDelegatedHandlers 里绑;转义走原有 escapeHtml。
        `<button class="na-chip" data-action="quick-ask" data-quick="${escapeHtml(a[1])}">${escapeHtml(a[0])}</button>`,
    )
    .join("");
  return `<div class="na-row"><span class="na-lbl">下一步</span>${chips}</div>`;
}

function autoGrow(t) {
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 160) + "px";
}
function onKey(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
}

function escapeHtml(s) {
  return (s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function addMsg(role, text) {
  const w = document.getElementById("welcome");
  if (w) w.remove();
  const chat = document.getElementById("chat");
  const t = new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
  const isAI = role === "ai";
  const div = document.createElement("div");
  div.className = "msg " + (isAI ? "ai" : "me");
  div.innerHTML = `
<div class="ava">${isAI ? "🐋" : userInitialChar()}</div>
<div class="bd">
  <div class="who">${isAI ? "ClawPool Agent" : "你"}<span class="t">${t}</span></div>
  <div class="ct">${isAI ? "" : escapeHtml(text)}</div>
</div>`;
  if (!chat.querySelector(".cwrap")) {
    chat.innerHTML = '<div class="cwrap" id="cwrap"></div>';
  }
  document.getElementById("cwrap").appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div.querySelector(".ct");
}

function userInitialChar() {
  const em = localStorage.getItem("oc_user_email") || "A";
  return (em[0] || "A").toUpperCase();
}

// 极简 Markdown 渲染(先 HTML 转义防注入,再处理 **粗体** / `代码` / 链接 / 换行)
function renderMd(s) {
  const esc = escapeHtml(s);
  const base = esc
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener" style="color:var(--brand2)">$1</a>',
    )
    .replace(/\n/g, "<br>");
  // 行情卡片(对齐主流 AI 助手的图表/widget 可视化):agent 回行情时是纯
  // 文本,这里从真实回复里抽出价格/涨跌/高低/量,渲染成涨跌色卡片。纯前端解析
  // agent 返回的真数据(不造数),抽不到就原样返回文本。
  const card = renderQuoteCard(s);
  return card ? card + base : base;
}

// 从行情回复文本里解析结构化数据,命中则返回一张涨跌色卡片 HTML,否则 null。
function renderQuoteCard(s) {
  const t = String(s || "");
  // 价格:$60,318 / 现价 $1,580
  const price = (t.match(/\$\s?([\d,]+(?:\.\d+)?)/) || [])[1];
  // 交易对:BTCUSDT / ETHUSDT / (平台现货 XXX)
  const sym = (t.match(/([A-Z]{2,10}USDT?)/) || [])[1];
  // 24h 变动:+0.33% / -0.20%
  const chg = (t.match(
    /24h\s*(?:变动|涨幅)?[:：]?\s*([+\-]?\d+(?:\.\d+)?)\s*%/,
  ) || [])[1];
  if (!price || !sym) return null; // 不是行情回复
  const high = (t.match(
    /(?:最高|24h\s*最高)[:：]?\s*\$?\s?([\d,]+(?:\.\d+)?)/,
  ) || [])[1];
  const low = (t.match(
    /(?:最低|24h\s*最低)[:：]?\s*\$?\s?([\d,]+(?:\.\d+)?)/,
  ) || [])[1];
  const vol = (t.match(
    /(?:成交[量额])[:：]?\s*(?:约)?\s*\$?\s?([\d,.]+\s*[亿万]?\s*(?:USDT)?)/,
  ) || [])[1];
  const up = chg == null ? null : parseFloat(chg) >= 0;
  const col = up == null ? "var(--brand2)" : up ? "#16c784" : "#ea3943";
  const arrow = up == null ? "" : up ? "▲" : "▼";
  const cell = (label, val) =>
    val
      ? `<div class="qc-cell"><div class="qc-k">${label}</div><div class="qc-v">${escapeHtml(val)}</div></div>`
      : "";
  return (
    `<div class="quote-card">` +
    `<div class="qc-head"><span class="qc-sym">${escapeHtml(sym)}</span>` +
    `<span class="qc-price">$${escapeHtml(price)}</span>` +
    (chg != null
      ? `<span class="qc-chg" style="color:${col}">${arrow} ${escapeHtml(chg)}%</span>`
      : "") +
    `</div><div class="qc-grid">` +
    cell("24h 高", high ? "$" + high : "") +
    cell("24h 低", low ? "$" + low : "") +
    cell("24h 量", vol) +
    `</div></div>`
  );
}

function showConn(msg, demo) {
  const b = document.getElementById("connBar");
  b.textContent = msg;
  b.className = "connbar show" + (demo ? " demo" : "");
}

// #187 转型:图片上传原本依赖 hub /files/upload-url 预签名 + agent 出图经
// hub /files/download-url 拿 S3 presigned URL。两级路由直连模式下 broker 端
// 未接管这条链,本 mini-app 不再暴露图片上传入口,直接提示"本期未开"。
async function onPickImage(ev) {
  ev.target.value = "";
  showConn(
    "本期新架构下暂未接图片上传通道(依赖后端 broker,后续独立 issue 补)。",
    true,
  );
}

async function send() {
  // Per-tenant isolation: you can only chat with a node you own. `active`
  // is set exclusively from the owner-scoped GET /tenants list (via
  // selectNode); the hub token then binds to that verified sub + tenant_id.
  // No node of your own → refuse and route NOWHERE (never a shared default).
  if (!active) {
    showConn(
      "你名下没有可用的 AI 节点。请联系管理员为你的账号开通节点后再对话。",
      true,
    );
    return;
  }
  const inp = document.getElementById("input");
  const text = inp.value.trim();
  if (!text) return;
  // #2 发送中门控:上一条还在途就忽略这次(不清输入、留着让用户等或改),
  // 给一条轻提示。避免连点/连发并发打爆 hub、避免气泡观感卡死。
  if (_isSending) {
    showConn("上一条还在回复中,请稍候…", false);
    return;
  }
  // #2 置位门控后,从这里到 finally 的所有代码(含 addMsg 等同步 DOM 操作)
  // 都在 try 内——任何一步抛错(如 DOM 未就绪)finally 都会复位 _isSending,
  // 绝不因 try 外抛错把用户永久锁死(review Warning 修正:enter/exit 对称绑定同一 try)。
  _isSending = true;
  _setSendingUI(true);
  let _thinkTimer = null;
  let ctEl = null;
  try {
    inp.value = "";
    autoGrow(inp);
    addMsg("me", text);
    history.push({ role: "user", content: text });
    ctEl = addMsg("ai", "");
    ctEl.innerHTML =
      '<span class="typing"><span></span><span></span><span></span></span>';
    // 思考过程面板(对齐主流 AI 助手 StatusParts):agent 处理期间,根据用户
    // 问题推断它在做什么,轮换显示步骤。首个 reply_delta 到达(callGateway 内 onDelta
    // 改 innerHTML)会自然覆盖掉它。纯前端,不依赖后端真实 step 事件。
    _thinkTimer = startThinking(ctEl, text);
    // 并发修复: this bubble is bound to its request via clientMessageId inside
    // callGateway (reply_delta/reply route to it by id). Multiple sends in
    // flight each keep their own bubble — no global, no cross-wiring.
    const res = await callGateway(active, text, ctEl);
    clearInterval(_thinkTimer);
    // callGateway 现在返回 {text, files}。兼容旧的纯字符串返回。
    const reply = typeof res === "string" ? res : res?.text || "";
    const files = typeof res === "string" ? [] : res?.files || [];
    // 护栏拦截识别:当回复是 LLM/Guardrail 错误占位(如 Bedrock Guardrail
    // 返回 400 Violated guardrail policy → 网关给出 "Something went wrong"
    // 占位)时,不当普通回复渲染,改成像主流 AI 助手那样的明确安全提示。
    // 例外:回复带图片(agent 出图)时即使文本为空也不算拦截。
    if (files.length === 0 && isGuardrailBlocked(reply)) {
      ctEl.innerHTML =
        '<div class="guard-block">🔒 这个请求触发了安全护栏,已被拦截。<br>' +
        '<span style="opacity:.7;font-size:.92em">涉及越权、数据外泄或越狱探测的请求会在平台层被阻断。换个问题试试,或聊聊行情、账户、策略。</span></div>';
      history.push({ role: "assistant", content: "[安全护栏拦截]" });
    } else if (files.length === 0 && isTransientError(reply)) {
      // 通用/瞬时错误(Something went wrong / TPS 限流)→ 中性"出错可重试",
      // 不误报护栏。实测后端同问题 localhost 答对,经 CloudFront 偶发此占位。
      ctEl.innerHTML =
        '<span style="opacity:.75">⚠️ 处理出错了,请再发一次试试。</span>';
      history.push({ role: "assistant", content: "[处理出错]" });
    } else if (files.length === 0 && !reply) {
      // 空回复(超时/agent 偶发/reply_error)→ 中性提示可重试,别误报护栏。
      ctEl.innerHTML =
        '<span style="opacity:.7">没收到回复,请再发一次。</span>';
      history.push({ role: "assistant", content: "[空回复]" });
    } else {
      ctEl.innerHTML =
        (reply ? renderMd(reply) : "") +
        renderFiles(files) +
        (reply ? renderNextActions(reply) : "");
      // 异步把 fileKey 换成预签名 URL 填进 <img>(agent 出图)。
      hydrateFiles(ctEl, files);
      history.push({
        role: "assistant",
        content: reply + (files.length ? ` [图片×${files.length}]` : ""),
      });
    }
  } catch (err) {
    clearInterval(_thinkTimer);
    // #13 主动丢弃(切换会话/断连时 _abortInflight reject)不是"连接失败":
    // 带 aborted 哨兵的 reject 静默跳过,不弹红色错误条(cancel ≠ error)。
    // finally 仍会复位门控(此时 _abortInflight 已复位,无害幂等)。
    if (err && err.aborted) return;
    const m = String(err);
    // ctEl 可能在创建前就抛错(DOM 未就绪)→ 判空,避免 catch 内二次抛错。
    if (ctEl && isGuardrailBlocked(m)) {
      ctEl.innerHTML =
        '<div class="guard-block">🔒 这个请求触发了安全护栏,已被拦截。</div>';
    } else if (ctEl) {
      ctEl.innerHTML = `<span style="color:var(--red)">[连接失败] </span>${escapeHtml(m.slice(0, 200))}`;
      showConn("连接失败:" + m.slice(0, 80), true);
    } else {
      showConn("发送失败:" + m.slice(0, 80), true);
    }
  } finally {
    // #2 无论成功/失败/超时,都解锁发送门控并恢复按钮,绝不把用户永久锁死。
    _isSending = false;
    _setSendingUI(false);
  }
}

// #2 发送中的 UI 态:禁用发送按钮 + 降透明度,给"在途"的可见反馈。
// callGateway 已有超时(见其内 timeout),finally 保证这里一定会解锁。
function _setSendingUI(sending) {
  const btn = document.getElementById("sendBtn");
  if (!btn) return;
  btn.disabled = !!sending;
  btn.style.opacity = sending ? "0.5" : "";
  btn.style.cursor = sending ? "not-allowed" : "";
}

// 通用/瞬时错误占位(OpenClaw "Something went wrong"、TPS 限流等)——
// 这不是安全护栏拦截!实测:同一行情查询 localhost 直连答对,浏览器经
// CloudFront 偶发拿到此占位(疑 Bedrock Guardrail ApplyGuardrail TPS 限流
// / session 偶发)。早期把它归进 isGuardrailBlocked,导致正常的"ETH 价位"
// 被误显示成"触发安全护栏",吓人且误导。拆出来按"出错可重试"中性处理。
function isTransientError(s) {
  if (!s) return false;
  const t = String(s).toLowerCase();
  return (
    t.includes("something went wrong") ||
    t.includes("/new to start") ||
    t.includes("too many requests") ||
    t.includes("try again")
  );
}
// 识别真正的安全护栏/内容过滤拦截信号(Bedrock Guardrail policy 命中)。
function isGuardrailBlocked(s) {
  // 空回复 ≠ 护栏(交给调用方中性处理);通用错误也不算护栏(见上)。
  if (!s) return false;
  const t = String(s).toLowerCase();
  return (
    t.includes("violated guardrail") ||
    t.includes("guardrail policy") ||
    t.includes("blocked by") ||
    t.includes("content filter")
  );
}

// ============ 对接 OpenClaw gateway(两级路由直连,#187 转型)============
// 生产:同源 POST /ws/{tenant_id}/v1/chat/completions,SSE 流式。
//   CloudFront /ws/* → ALB LOR → OpenResty edge(查 Redis 拿 host:port)→
//   host iptables DNAT → microVM:18789 OpenClaw gateway。
// 认证:Authorization: Bearer <gateway_token>。token 明文由租户从 console
//   reveal 页面拷贝到 chat 页(localStorage.oc_gw_token),per-租户唯一。
// demo:POST 本地代理 /chat(无 token,代理内存持 token)。

// 发送并发门控:一条消息在途时不受理新 send(),避免连点、避免死气泡。
let _isSending = false;
// 在途 SSE 请求:每条消息挂一个 AbortController,newSession()/切换 node 时
// abort 全部,气泡以 aborted 哨兵 reject → send() catch 静默跳过。
let _inflight = new Set();
function _abortInflight(reason) {
  for (const ctrl of _inflight) {
    try {
      ctrl._reason = reason || "已切换";
      ctrl.abort();
    } catch (_) {}
  }
  _inflight.clear();
  _isSending = false;
  if (typeof _setSendingUI === "function") _setSendingUI(false);
}
// #187 转型下 attach/hydrate 图片链路未接入,agent 出图相关的 files 数组不
// 再流转;为了兼容 send() 里对 files 长度的引用,给一个恒返回空串的存根。
function renderFiles() {
  return "";
}
function hydrateFiles() {
  // no-op:两级路由直连模式下 gateway 不吐 fileKey;图片链路待独立 issue。
}

// gateway token(明文 Bearer)。租户在 console reveal 页面拿到密文 →
// 自解后贴到此 localStorage。空 = 让用户点击「输入 gateway token」引导。
function gwToken() {
  return window.OC_GW_TOKEN || localStorage.getItem("oc_gw_token") || "";
}
function promptGwToken() {
  const t = window.prompt(
    "输入 gateway token(在 Console 的租户详情 reveal 处得到,自 KMS 解密后的明文;仅存本浏览器 localStorage,不写入源码):",
  );
  if (t && t.trim()) {
    localStorage.setItem("oc_gw_token", t.trim());
    showConn("已保存 gateway token,可以发消息了。", false);
  }
}

// 数据面 URL:同源经 CloudFront /ws/* → ALB → OpenResty → host DNAT → gateway。
// 可被 window.OC_GW_BASE 覆盖(测试环境直连 host 时用)。
function gatewayUrl(tenantId, path) {
  const base = (window.OC_GW_BASE || ORIGIN).replace(/\/+$/, "");
  return `${base}/ws/${encodeURIComponent(tenantId)}${path}`;
}

// 从 SSE data 帧解析 delta:content 字段或 choices[0].delta.content。返回文本片段。
function _parseSseDelta(dataStr) {
  if (!dataStr || dataStr === "[DONE]") return null;
  try {
    const o = JSON.parse(dataStr);
    if (o && typeof o === "object") {
      if (o.choices && o.choices[0]) {
        const d = o.choices[0].delta || o.choices[0].message || {};
        if (typeof d.content === "string") return d.content;
      }
      if (typeof o.content === "string") return o.content;
    }
  } catch (_) {}
  return null;
}

// callGateway:同源 POST /ws/{tenant_id}/v1/chat/completions,SSE 流式。
// 累积 delta 到 ctEl(边收边渲染),SSE 结束后返回全文 { text, files }。
// files 恒空(agent 出图链路未接入,见 renderFiles 存根)。
async function callGateway(node, text, ctEl) {
  if (IS_LOCAL) {
    const r = await fetch(`${PROXY}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        node: node.id,
        message: text,
        history: history.slice(-12),
      }),
    });
    if (!r.ok) {
      let detail = "HTTP " + r.status;
      try {
        const e = await r.json();
        detail = e.error || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    return { text: d.reply || d.content || JSON.stringify(d), files: [] };
  }

  const token = gwToken();
  if (!token) {
    throw new Error(
      "未配置 gateway token(点右上方「gateway token」输入,或从 console reveal 页面复制)",
    );
  }
  const controller = new AbortController();
  _inflight.add(controller);
  const to = setTimeout(() => {
    controller._reason = "回复超时";
    controller.abort();
  }, 120000);
  try {
    const r = await fetch(gatewayUrl(node.tenant, "/v1/chat/completions"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + token,
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        model: "default",
        stream: true,
        messages: history
          .slice(-12)
          .map((m) => ({ role: m.role, content: m.content }))
          .concat([{ role: "user", content: text }]),
      }),
      signal: controller.signal,
    });
    if (!r.ok) {
      let detail = "HTTP " + r.status;
      try {
        const e = await r.json();
        detail = e.error?.message || e.error || e.message || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    // 非 SSE(如错误页 / 非 stream):按 JSON 一次性读取
    const ctype = r.headers.get("content-type") || "";
    if (!ctype.includes("event-stream")) {
      const j = await r.json();
      const full =
        j?.choices?.[0]?.message?.content ||
        j?.content ||
        j?.reply ||
        JSON.stringify(j);
      if (ctEl) ctEl.innerHTML = renderMd(full);
      return { text: full, files: [] };
    }
    // SSE 流式:边收边累积
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let full = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        // 取每行以 "data:" 开头的部分,拼在一起
        const dataLines = frame
          .split("\n")
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).trimStart())
          .join("\n");
        if (!dataLines) continue;
        if (dataLines === "[DONE]") continue;
        const piece = _parseSseDelta(dataLines);
        if (piece) {
          full += piece;
          if (ctEl) {
            ctEl.innerHTML = renderMd(full);
            const chat = document.getElementById("chat");
            if (chat) chat.scrollTop = chat.scrollHeight;
          }
        }
      }
    }
    return { text: full, files: [] };
  } catch (e) {
    if (e && e.name === "AbortError") {
      const err = new Error(controller._reason || "请求已取消");
      if (controller._reason !== "回复超时") err.aborted = true;
      throw err;
    }
    throw e;
  } finally {
    clearTimeout(to);
    _inflight.delete(controller);
  }
}

// ============ 启动 ============
(async function boot() {
  // 等鉴权门(code→token 换取是 async)完成再初始化,避免在 token 存好
  // 之前就 loadNodes()/连 hub 拿不到 token,也避免与 authGate 竞争重载。
  try {
    await window.__authReady;
  } catch (_) {}
  // 鉴权彻底失败(连续换取失败)→ 显示错误而不是空转刷新。
  if (window.__authError) {
    const bx = document.getElementById("boot");
    if (bx) bx.querySelector(".tx").textContent = window.__authError;
    return;
  }
  // 解析登录用户(从 id_token 的 payload 取 email/sub,仅展示用)
  let email = "";
  try {
    const tok = idToken();
    if (tok) {
      const payload = JSON.parse(
        atob(tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")),
      );
      email =
        payload.email || payload["cognito:username"] || payload.sub || "";
    }
  } catch (_) {}
  if (IS_LOCAL && !email) email = "本地 demo";
  if (email) {
    localStorage.setItem("oc_user_email", email);
    document.getElementById("userEmail").textContent = email;
    document.getElementById("userInitial").textContent = (
      email[0] || "A"
    ).toUpperCase();
  }
  if (IS_LOCAL) {
    document.getElementById("logoutBtn").style.display = "none";
  }

  // 显示 app,移除 boot 遮罩
  document.getElementById("boot").style.display = "none";
  document.getElementById("appRoot").style.display = "grid";

  // 拉节点
  loadNodes();

  // #41 会话历史回看: 刷新/重开页面且 thread 已存在 → 后台自动拉回本对话历史,
  // 让对话跨刷新存活。失败静默(loadHistory 内部已处理提示),不阻塞首屏。
  if (_threadPreexisted && !IS_LOCAL) {
    loadHistory().catch(() => {});
  }

  // demo 模式探测本地代理
  if (IS_LOCAL) {
    fetch(`${PROXY}/health`)
      .then((r) => r.json())
      .then((d) => {
        if (d && d.ok)
          showConn(
            "已连接 OpenClaw gateway · 经本地代理(token 不出代理)",
            false,
          );
      })
      .catch(() =>
        showConn("本地代理未连接 — 启动 SSH 隧道 + 代理后刷新", true),
      );
  }
})();
function logout() {
  localStorage.removeItem("oc_id_token");
  localStorage.removeItem("oc_token_exp");
  localStorage.removeItem("oc_user_email");
  // 回 Hosted UI logout,再跳回登录
  const domain = window.OC_COGNITO_DOMAIN;
  const clientId = window.OC_COGNITO_CLIENT_ID;
  const logoutUri = window.OC_COGNITO_REDIRECT_URI;
  window.location.href =
    "https://" +
    domain +
    "/logout?client_id=" +
    clientId +
    "&logout_uri=" +
    encodeURIComponent(logoutUri);
}

// #63 CSP wiring — 事件委托代替内联 on*= 属性。
// 静态元素用 data-action 分派;动态渲染出来的元素(nodeList / quickAsk chip /
// provisionBtn / promptKey / composer icons)也走同一个委托,不需重绑。
// 组合 payload 从 data-* 读,避免拼字符串到 innerHTML 里的 onclick=。
(function setupDelegatedHandlers() {
  // 分派表:data-action="X" 触发 handler。参数从 data-* 读,严格白名单。
  const ACTIONS = {
    "new-session": function () {
      newSession();
    },
    "load-history": function () {
      loadHistory();
    },
    "load-nodes": function () {
      loadNodes();
    },
    logout: function () {
      logout();
    },
    "prompt-key": function () {
      promptKey();
    },
    "provision-node": function () {
      provisionMyNode();
    },
    "quick-ask": function (el) {
      var t = el.getAttribute("data-quick");
      if (t) quickAsk(t);
    },
    "select-node": function (el) {
      var id = el.getAttribute("data-node-id");
      if (id) selectNode(id);
    },
    "pick-image": function () {
      var f = document.getElementById("fileInput");
      if (f) f.click();
    },
    send: function () {
      send();
    },
  };

  document.addEventListener("click", function (ev) {
    var el = ev.target.closest("[data-action]");
    if (!el) return;
    var act = el.getAttribute("data-action");
    var fn = ACTIONS[act];
    if (fn) fn(el, ev);
  });

  // textarea:发送键位与自动高度
  var input = document.getElementById("input");
  if (input) {
    input.addEventListener("input", function () {
      autoGrow(input);
    });
    input.addEventListener("keydown", onKey);
  }

  // 隐藏 file input 的 change
  var fi = document.getElementById("fileInput");
  if (fi) fi.addEventListener("change", onPickImage);
})();
