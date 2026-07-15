// Issue #16 — Cognito OAuth2 authorization-code + PKCE flow.
// Replaces the legacy implicit flow (response_type=token) which only returned
// a 1h id_token with no refresh capability.
//
// Flow:
//   1. No valid token → generate PKCE verifier + S256 challenge → redirect to Hosted UI
//   2. Callback ?code= → POST /oauth2/token with code_verifier → get id/refresh tokens
//   3. Token near expiry → silent refresh via refresh_token (7-day window)
//
// FOUC prevention: page is hidden until auth resolves (success or redirect).

// Hide page immediately to prevent unauthorized UI flash (FOUC).
document.documentElement.style.display = "none";

(async function authGate() {
  var domain = window.OC_COGNITO_DOMAIN;
  var clientId = window.OC_COGNITO_CLIENT_ID;
  var redirectUri = window.OC_COGNITO_REDIRECT_URI;

  // Auth not configured (local dev) — show page immediately.
  if (!domain || !clientId) {
    document.documentElement.style.display = "";
    return;
  }

  // ── PKCE helpers ──────────────────────────────────────────────────────────

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

  // ── Token exchange (POST /oauth2/token) ───────────────────────────────────

  async function _tokenExchange(body) {
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
    localStorage.setItem(
      "oc_token_exp",
      String(Date.now() + parseInt(t.expires_in || "3600", 10) * 1000),
    );
  }

  // ── Silent refresh with concurrency lock ──────────────────────────────────
  // Multiple 401s may call refreshIdToken() concurrently; the lock ensures
  // only one actual request goes to Cognito.

  var _refreshPromise = null;

  async function _doRefresh() {
    var rt = localStorage.getItem("oc_refresh_token");
    if (!rt) return null;
    try {
      var t = await _tokenExchange({
        grant_type: "refresh_token",
        client_id: clientId,
        refresh_token: rt,
      });
      _storeTokens(t);
      return t.id_token || localStorage.getItem("oc_id_token");
    } catch (e) {
      // Refresh token expired/revoked — hard clear and force re-login.
      localStorage.removeItem("oc_id_token");
      localStorage.removeItem("oc_refresh_token");
      localStorage.removeItem("oc_token_exp");
      return null;
    }
  }

  async function refreshIdToken() {
    if (_refreshPromise) return _refreshPromise;
    _refreshPromise = _doRefresh().finally(function () {
      _refreshPromise = null;
    });
    return _refreshPromise;
  }

  // Expose globally so app.core.js can call it on API 401.
  window.refreshIdToken = refreshIdToken;

  // ── Login redirect (PKCE) ─────────────────────────────────────────────────

  async function gotoLogin() {
    var verifier = _randVerifier();
    sessionStorage.setItem("oc_pkce_verifier", verifier);
    var chal = await _challenge(verifier);
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
  }

  // ── Auth gate main logic ──────────────────────────────────────────────────

  // 1. Already have a valid (non-expired) token → allow access.
  //    Also strip any leftover ?code= from URL to prevent stale-code loops.
  var token0 = localStorage.getItem("oc_id_token");
  var exp0 = parseInt(localStorage.getItem("oc_token_exp") || "0", 10);
  if (token0 && exp0 > Date.now()) {
    if (window.location.search) history.replaceState({}, "", redirectUri);
    document.documentElement.style.display = "";
    return;
  }

  // 2. Callback: ?code= present → exchange for tokens using PKCE verifier.
  var qs = new URLSearchParams(window.location.search);
  var code = qs.get("code");
  if (code) {
    // Strip ?code= from URL immediately to prevent re-use on reload.
    history.replaceState({}, "", redirectUri);
    var verifier = sessionStorage.getItem("oc_pkce_verifier");
    if (verifier) {
      try {
        var t = await _tokenExchange({
          grant_type: "authorization_code",
          client_id: clientId,
          code: code,
          redirect_uri: redirectUri,
          code_verifier: verifier,
        });
        _storeTokens(t);
        sessionStorage.removeItem("oc_pkce_verifier");
        document.documentElement.style.display = "";
        return;
      } catch (e) {
        sessionStorage.removeItem("oc_pkce_verifier");
        // Prevent infinite redirect loop: count consecutive failures.
        var fails =
          parseInt(sessionStorage.getItem("oc_auth_fails") || "0", 10) + 1;
        sessionStorage.setItem("oc_auth_fails", String(fails));
        if (fails < 2) {
          await gotoLogin();
          return;
        }
        // Too many failures — show error, stop redirecting.
        sessionStorage.removeItem("oc_auth_fails");
        document.documentElement.style.display = "";
        document.body.innerHTML =
          '<div style="padding:2rem;text-align:center;color:#e53;font-family:sans-serif">' +
          "<h2>登录失败</h2><p>Token 换取失败，请刷新页面重试或联系管理员。</p></div>";
        return;
      }
    }
    // No verifier in session (e.g. opened callback URL in new tab) → re-login.
    await gotoLogin();
    return;
  }

  // 3. No code, no valid token — try silent refresh if refresh_token exists.
  var refreshed = await refreshIdToken();
  if (refreshed) {
    document.documentElement.style.display = "";
    return;
  }

  // 4. Nothing works — redirect to Cognito login with PKCE.
  sessionStorage.removeItem("oc_auth_fails");
  await gotoLogin();
})();
