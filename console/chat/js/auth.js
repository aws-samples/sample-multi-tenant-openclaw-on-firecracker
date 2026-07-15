// Cognito OAuth2 authorization-code + PKCE 鉴权门。
// 未登录 -> 跳 Hosted UI(code+PKCE);回调用 code 换 id/refresh token;
// id_token 临过期用 refresh_token 静默换新(7天免登录),不踢回登录页。
// 治根因:旧 implicit flow 只拿 id_token(1h过期)、无 refresh,过期就掉线。
//
// #63 (CSP 外链化) — 从 index.html 内联 <script> 搬出,配合
// CloudFront ResponseHeadersPolicy 的 script-src 'self'。账号相关值
// 由部署时 setup.sh 注入到 index.html 里的
// <script type="application/json" id="oc-config">,本文件读之(JSON script
// 不是可执行脚本,不受 CSP script-src 约束)。
(function () {
  // 读部署时注入的账号配置(setup.sh sed 目标)。JSON.parse 失败或占位未替换 => 空。
  var _cfg = {};
  try {
    var _el = document.getElementById("oc-config");
    if (_el) _cfg = JSON.parse(_el.textContent || _el.innerText || "{}");
  } catch (e) {
    _cfg = {};
  }
  var _ph = function (v) {
    v = v || "";
    return v.indexOf("__OC_") === 0 ? "" : v;
  };
  window.OC_COGNITO_DOMAIN = _ph(_cfg.cognito_domain);
  window.OC_COGNITO_CLIENT_ID = _ph(_cfg.cognito_client_id);
  // redirect 按当前页路径自适应:chat 页回 /chat/index.html、console 回 /console,
  // 一套注入值两页通用,解「一份 config 给不了两个 redirect」冲突。
  window.OC_COGNITO_REDIRECT_URI =
    window.location.origin + window.location.pathname;
  // 控制面 API(跨域,CORS 已对全网放行)。用于动态拉当前用户的节点列表。
  window.OC_API_URL = _ph(_cfg.api_url);
  // API Gateway 限流 key(usage-plan key,非授权凭据)。部署时注入真值,git 不写死。
  // 空值回退 localStorage.oc_api_key。授权靠 Cognito+owner。
  window.OC_API_KEY = _ph(_cfg.api_key);
  // SECURITY (per-tenant isolation, no shared fallback): there is NO default
  // tenant. A user may ONLY chat with a node that `GET /tenants` returns for
  // them — that list is owner-scoped server-side (handler.py owner filter +
  // hub authorizeSubForTenant). If a logged-in user has no running node of
  // their own, the UI shows an explicit "no node" state and routes NOWHERE;
  // it must never fall back to a shared/hardcoded tenant (that would route
  // unrelated users into the same node — a cross-tenant breach). Do NOT
  // reintroduce a window.OC_DEFAULT_TENANT or any hardcoded tenant id here.
  window.OC_DEFAULT_TENANT = "";

  // PKCE helpers (code_verifier / S256 challenge).
  function _b64url(buf) {
    return btoa(String.fromCharCode.apply(null, new Uint8Array(buf)))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  }
  function _randVerifier() {
    var a = new Uint8Array(32);
    crypto.getRandomValues(a);
    return _b64url(a.buffer);
  }
  async function _challenge(verifier) {
    var d = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(verifier),
    );
    return _b64url(d);
  }
  // POST to Cognito /oauth2/token. grant: "authorization_code" | "refresh_token".
  async function _tokenExchange(domain, clientId, redirectUri, body) {
    var r = await fetch("https://" + domain + "/oauth2/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(body).toString(),
    });
    if (!r.ok) throw new Error("token endpoint HTTP " + r.status);
    return await r.json();
  }
  function _storeTokens(t) {
    if (t.id_token) localStorage.setItem("oc_id_token", t.id_token);
    if (t.refresh_token)
      localStorage.setItem("oc_refresh_token", t.refresh_token);
    // id/access tokens are short-lived (≤24h); record absolute expiry so
    // we can refresh proactively before the hub /token call would 401.
    localStorage.setItem(
      "oc_token_exp",
      Date.now() + parseInt(t.expires_in || "3600", 10) * 1000,
    );
  }
  // Silent refresh: swap a valid refresh_token for a fresh id_token. Returns
  // the new id_token or null (caller falls back to interactive login).
  async function refreshIdToken() {
    var rt = localStorage.getItem("oc_refresh_token");
    if (!rt) return null;
    try {
      var t = await _tokenExchange(
        window.OC_COGNITO_DOMAIN,
        window.OC_COGNITO_CLIENT_ID,
        window.OC_COGNITO_REDIRECT_URI,
        {
          grant_type: "refresh_token",
          client_id: window.OC_COGNITO_CLIENT_ID,
          refresh_token: rt,
        },
      );
      // refresh-token responses omit refresh_token unless rotation is on;
      // _storeTokens keeps the existing one when absent.
      _storeTokens(t);
      return t.id_token || localStorage.getItem("oc_id_token");
    } catch (e) {
      return null;
    }
  }
  window.refreshIdToken = refreshIdToken;

  // authGate is async because the code→token exchange awaits a network call.
  // The boot overlay stays until it resolves; index code runs after.
  window.__authReady = (async function authGate() {
    var host = window.location.hostname;
    var isLocal =
      window.location.protocol === "file:" ||
      host === "localhost" ||
      host === "127.0.0.1";
    if (isLocal) {
      window.OC_LOCAL = true;
      return;
    }

    var domain = window.OC_COGNITO_DOMAIN;
    var clientId = window.OC_COGNITO_CLIENT_ID;
    var redirectUri = window.OC_COGNITO_REDIRECT_URI;

    function gotoLogin() {
      // generate + persist PKCE verifier, redirect to Hosted UI with S256.
      var verifier = _randVerifier();
      sessionStorage.setItem("oc_pkce_verifier", verifier);
      _challenge(verifier).then(function (chal) {
        var url =
          "https://" +
          domain +
          "/oauth2/authorize?client_id=" +
          clientId +
          "&response_type=code&scope=openid+email&redirect_uri=" +
          encodeURIComponent(redirectUri) +
          "&code_challenge_method=S256&code_challenge=" +
          chal;
        window.location.href = url;
      });
    }

    // 防御 1:已有未过期 id_token → 先清掉 URL 残留的 ?code= 再放行。
    // authorization code 一次性,带着旧 code reload 会二次换取失败 → 死循环;
    // 有有效 token 时根本不该再碰 code。这条最先判,斩断循环。
    var token0 = localStorage.getItem("oc_id_token");
    var exp0 = parseInt(localStorage.getItem("oc_token_exp") || "0", 10);
    if (token0 && exp0 > Date.now()) {
      if (window.location.search) history.replaceState({}, "", redirectUri);
      return;
    }

    // 回调:?code= → 用 PKCE verifier 换 id/refresh token
    var qs = new URLSearchParams(window.location.search);
    var code = qs.get("code");
    if (code) {
      // 防御 2:换取前先把 ?code= 从地址栏抹掉,任何后续 reload 都不会
      // 拿同一个(已消费的)code 再换一次。
      history.replaceState({}, "", redirectUri);
      var verifier = sessionStorage.getItem("oc_pkce_verifier");
      if (verifier) {
        try {
          var t = await _tokenExchange(domain, clientId, redirectUri, {
            grant_type: "authorization_code",
            client_id: clientId,
            code: code,
            redirect_uri: redirectUri,
            code_verifier: verifier,
          });
          _storeTokens(t);
          sessionStorage.removeItem("oc_pkce_verifier");
          return;
        } catch (e) {
          sessionStorage.removeItem("oc_pkce_verifier");
          // 防御 3:换取失败不立刻再跳登录(否则 Cognito 有 session 会
          // 秒发新 code 形成高速循环)。用 sessionStorage 计数,>=2 次
          // 连续失败就停在错误态让用户手动重试,不无限刷新。
          var fails =
            parseInt(sessionStorage.getItem("oc_auth_fails") || "0", 10) + 1;
          sessionStorage.setItem("oc_auth_fails", String(fails));
          if (fails < 2) {
            gotoLogin();
          } else {
            sessionStorage.removeItem("oc_auth_fails");
            window.__authError = "登录换取失败,请刷新页面重试";
          }
          return;
        }
      }
    }
    // 走到这说明无有效 token、也没有可换的 code → 清零失败计数
    sessionStorage.removeItem("oc_auth_fails");

    // 有未过期 id_token → 放行(冗余兜底,正常已被防御1拦下)
    var token = localStorage.getItem("oc_id_token");
    var exp = parseInt(localStorage.getItem("oc_token_exp") || "0", 10);
    if (token && exp > Date.now()) return;

    // id_token 过期但有 refresh_token → 静默换新(7天内不踢登录)
    if (localStorage.getItem("oc_refresh_token")) {
      var fresh = await refreshIdToken();
      if (fresh) return;
    }

    // 无 token 且无法刷新 → 跳 Hosted UI
    localStorage.removeItem("oc_id_token");
    gotoLogin();
  })();
})();
