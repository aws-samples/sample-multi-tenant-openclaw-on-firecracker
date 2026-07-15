// RSA 出站凭据(asymmetric-v1):调用方本地持 RSA-4096 私钥,注册公钥到平台;
// GET /tenants/{id}/credentials 拿回 enc:v1: 信封,浏览器本地用私钥 OAEP-SHA256 解密。
// 全程 WebCrypto(浏览器原生,零依赖);私钥只在 localStorage,绝不上传、绝不进 curl 导出。
// 与平台 core/envelope.py(alg_code=1 = RSA_4096_OAEP_SHA_256)+ scripts/oc-outbound-cred-demo.py 对称。
window.ocRsa = {
  // 本地 keypair 状态(PEM 文本)+ 平台当前 recipient key 元数据 + 解密结果。
  rsaPrivPem: "",
  rsaPubPem: "",
  rsaRecipientKey: null, // GET /recipient-key 返回的平台当前公钥元数据
  rsaBusy: false,
  rsaMsg: "",
  rsaSelectedTenant: "",
  // ── 入站(inbound)状态:平台公钥 + 本地加密的 name=value 凭据 ──────────────
  inboundPubKey: null, // GET /clawpool-rsa-public-key 返回 { public_key_pem, key_id, ... }
  inboundItems: [], // [{ field, value }] 用户填的明文凭据(编码后即清)
  inboundEncrypted: null, // { scheme:'asymmetric-v1', items:{field: enc:v1:...} }
  inboundNewField: "",
  inboundNewValue: "",
  inboundCurl: "", // exportInboundCurl 生成的 curl 文本(供页面 textarea 复制,只含密文)
  // 解密结果:{ tenantId, gatewayToken, deviceId, devicePrivPem, devicePub, scopes, enc }
  rsaCreds: null,

  initRsa() {
    // 私钥留在浏览器 localStorage(demo);生产应放调用方的 KMS/文件保险箱。
    this.rsaPrivPem = localStorage.getItem("oc_rsa_priv_pem") || "";
    this.rsaPubPem = localStorage.getItem("oc_rsa_pub_pem") || "";
  },

  _rsaFlash(msg, ms = 3000) {
    this.rsaMsg = msg;
    setTimeout(() => (this.rsaMsg = ""), ms);
  },

  // ── PEM ↔ ArrayBuffer helpers ─────────────────────────────────────────
  _pemToDer(pem) {
    const b64 = pem
      .replace(/-----BEGIN [^-]+-----/, "")
      .replace(/-----END [^-]+-----/, "")
      .replace(/\s+/g, "");
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  },
  _derToPem(der, label) {
    const bytes = new Uint8Array(der);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    const b64 = btoa(bin);
    const lines = b64.match(/.{1,64}/g).join("\n");
    return `-----BEGIN ${label}-----\n${lines}\n-----END ${label}-----\n`;
  },

  // ── 1) 首次生成 RSA-4096 keypair(私钥自留,公钥待注册)────────────────
  async generateRsaKeypair() {
    if (
      this.rsaPrivPem &&
      !confirm(
        "已存在本地私钥,重新生成会覆盖旧私钥(旧 credentials 将无法解密)。继续?",
      )
    )
      return;
    this.rsaBusy = true;
    try {
      const kp = await crypto.subtle.generateKey(
        {
          name: "RSA-OAEP",
          modulusLength: 4096,
          publicExponent: new Uint8Array([1, 0, 1]),
          hash: "SHA-256",
        },
        true, // extractable — 需导出 PKCS8 私钥 + SPKI 公钥
        ["decrypt"],
      );
      const spki = await crypto.subtle.exportKey("spki", kp.publicKey);
      const pkcs8 = await crypto.subtle.exportKey("pkcs8", kp.privateKey);
      this.rsaPubPem = this._derToPem(spki, "PUBLIC KEY");
      this.rsaPrivPem = this._derToPem(pkcs8, "PRIVATE KEY");
      localStorage.setItem("oc_rsa_pub_pem", this.rsaPubPem);
      localStorage.setItem("oc_rsa_priv_pem", this.rsaPrivPem);
      this._rsaFlash("✓ 已生成 RSA-4096 keypair(私钥留在本浏览器,从不上传)");
    } catch (e) {
      this._rsaFlash("✗ 生成失败: " + (e && e.message));
    } finally {
      this.rsaBusy = false;
    }
  },

  // ── 2) 注册公钥到平台(POST /recipient-key, admin-only)────────────────
  async registerRsaPubkey() {
    if (!this.rsaPubPem) return this._rsaFlash("✗ 先生成 keypair");
    this.rsaBusy = true;
    try {
      const r = await this.api("POST", "recipient-key", {
        public_key_pem: this.rsaPubPem,
        source: "console",
      });
      if (r && r.error) return this._rsaFlash("✗ " + r.error);
      await this.loadRecipientKey();
      this._rsaFlash(
        "✓ 公钥已注册 key_id=" +
          (r.key_id || (r.recipient_key || {}).key_id || "?"),
      );
    } catch (e) {
      this._rsaFlash("✗ 注册失败: " + (e && e.message));
    } finally {
      this.rsaBusy = false;
    }
  },

  // ── 3) 获取平台当前 recipient 公钥(GET /recipient-key)─────────────────
  async loadRecipientKey() {
    if (!this.apiUrl || !this.apiKey) return;
    try {
      const r = await this.api("GET", "recipient-key");
      this.rsaRecipientKey = (r && r.recipient_key) || null;
    } catch {
      this.rsaRecipientKey = null;
    }
  },

  // ── 4) 禁用当前 recipient key(轮换,POST /recipient-key/disable)────────
  async disableRsaKey() {
    if (
      !confirm(
        "禁用平台当前 recipient key?禁用后 GET credentials 会 409,需重新注册。",
      )
    )
      return;
    this.rsaBusy = true;
    try {
      const r = await this.api("POST", "recipient-key/disable");
      if (r && r.error) return this._rsaFlash("✗ " + r.error);
      await this.loadRecipientKey();
      this._rsaFlash("✓ 已禁用当前 recipient key");
    } catch (e) {
      this._rsaFlash("✗ " + (e && e.message));
    } finally {
      this.rsaBusy = false;
    }
  },

  // ── enc:v1: 信封解析(与 oc-outbound-cred-demo.parse_enc_v1 对称)────────
  _parseEncV1(value) {
    if (typeof value !== "string" || !value.startsWith("enc:v1:")) return null;
    const parts = value.split(":");
    if (parts.length !== 6) throw new Error("malformed enc:v1 envelope");
    const [, , alg, , hybrid, bodyB64] = parts;
    if (alg !== "1" || hybrid !== "0")
      throw new Error(
        `console 只解 alg=1 hybrid=0 (得到 alg=${alg} hybrid=${hybrid})`,
      );
    const bin = atob(bodyB64);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  },

  async _rsaDecrypt(privKey, cipherBuf) {
    const plain = await crypto.subtle.decrypt(
      { name: "RSA-OAEP" },
      privKey,
      cipherBuf,
    );
    return new TextDecoder().decode(plain);
  },

  // ── 5) 拿租户 credentials → 本地私钥解密(GET /tenants/{id}/credentials)──
  async fetchAndDecryptCreds(tenantId) {
    if (!tenantId) return;
    if (!this.rsaPrivPem)
      return this._rsaFlash("✗ 本地无私钥,先在 RSA 凭据页生成 keypair");
    this.rsaBusy = true;
    this.rsaCreds = null;
    try {
      const r = await this.api("GET", "tenants/" + tenantId + "/credentials");
      if (r && r.error) return this._rsaFlash("✗ " + r.error);
      const cc = r.claw_credentials;
      if (!cc)
        return this._rsaFlash(
          "✗ 未拿到 claw_credentials(该租户未 mint device/gateway 或 asymmetric-v1 未启用)",
        );

      const privKey = await crypto.subtle.importKey(
        "pkcs8",
        this._pemToDer(this.rsaPrivPem),
        { name: "RSA-OAEP", hash: "SHA-256" },
        false,
        ["decrypt"],
      );

      const gwEnc = (cc.gateway || {}).token || "";
      const dev = cc.device || {};
      let gatewayToken = "";
      let devicePrivPem = "";
      if (gwEnc) {
        const ct = this._parseEncV1(gwEnc);
        if (ct) gatewayToken = await this._rsaDecrypt(privKey, ct);
      }
      if (dev.private_key) {
        const ct = this._parseEncV1(dev.private_key);
        if (ct) devicePrivPem = await this._rsaDecrypt(privKey, ct);
      }
      this.rsaCreds = {
        tenantId,
        enc: cc.enc || {},
        gatewayToken,
        deviceId: dev.id || "",
        devicePub: dev.public_key || "",
        devicePrivPem,
        scopes: dev.scopes || [],
      };
      this._rsaFlash("✓ credentials 解密成功(本地私钥,明文不离开浏览器)");
    } catch (e) {
      this._rsaFlash("✗ 解密失败: " + (e && e.message));
    } finally {
      this.rsaBusy = false;
    }
  },

  clearRsaCreds() {
    this.rsaCreds = null;
  },

  // ══ 入站 flow(asymmetric-v1):平台公钥本地加密业务凭据 → 建租户注入 ══════
  // 与出站相反方向:调用方拿【平台】RSA 公钥,浏览器 OAEP-SHA256 加密 name=value,
  // POST /tenants 带 env_injected_credentials;host 在 VM launch 时 kms:Decrypt 注入。
  // 调用方零 AWS 依赖、不碰 kms(只用公钥加密)。明文凭据不上传、不进 curl 导出。

  // 1) 取平台入站公钥(GET /clawpool-rsa-public-key)
  async loadInboundPubKey() {
    if (!this.apiUrl || !this.apiKey) return this._rsaFlash("✗ 先配 API");
    this.rsaBusy = true;
    try {
      const r = await this.api("GET", "clawpool-rsa-public-key");
      if (r && r.error) return this._rsaFlash("✗ " + r.error);
      if (!r.public_key_pem)
        return this._rsaFlash("✗ 平台未开 asymmetric-v1(无 RSA CMK 公钥)");
      this.inboundPubKey = r;
      this._rsaFlash("✓ 已取平台公钥 key_id=" + (r.key_id || "?"));
    } catch (e) {
      this._rsaFlash("✗ " + (e && e.message));
    } finally {
      this.rsaBusy = false;
    }
  },

  addInboundItem() {
    const f = (this.inboundNewField || "").trim();
    const v = this.inboundNewValue || "";
    if (!f || !v) return this._rsaFlash("✗ field 和 value 都要填");
    // field 名匹配 registry(小写字母/数字/下划线)
    if (!/^[a-z][a-z0-9_]*$/.test(f))
      return this._rsaFlash("✗ field 名须小写字母开头(a-z0-9_)");
    this.inboundItems.push({ field: f, value: v });
    this.inboundNewField = "";
    this.inboundNewValue = "";
  },
  removeInboundItem(i) {
    this.inboundItems.splice(i, 1);
  },

  // 2) 本地用平台公钥 OAEP-SHA256 加密每条 → enc:v1:1:<key_id>:0:<b64>
  async encryptInbound() {
    if (!this.inboundPubKey) return this._rsaFlash("✗ 先取平台公钥");
    if (!this.inboundItems.length) return this._rsaFlash("✗ 先加至少一条凭据");
    this.rsaBusy = true;
    try {
      const pubKey = await crypto.subtle.importKey(
        "spki",
        this._pemToDer(this.inboundPubKey.public_key_pem),
        { name: "RSA-OAEP", hash: "SHA-256" },
        false,
        ["encrypt"],
      );
      const keyId = this.inboundPubKey.key_id || "clawpool";
      const items = {};
      for (const it of this.inboundItems) {
        const plain = new TextEncoder().encode(it.value);
        // RSA-4096 OAEP-SHA256 明文上限 446B,超了拒(与平台契约一致)
        if (plain.length > 446)
          throw new Error(
            `${it.field} 明文 ${plain.length}B 超 RSA-OAEP 上限 446B`,
          );
        const ct = await crypto.subtle.encrypt(
          { name: "RSA-OAEP" },
          pubKey,
          plain,
        );
        const b64 = btoa(String.fromCharCode(...new Uint8Array(ct)));
        items[it.field] = `enc:v1:1:${keyId}:0:${b64}`;
      }
      this.inboundEncrypted = { scheme: "asymmetric-v1", items };
      this._rsaFlash(
        `✓ 已本地加密 ${this.inboundItems.length} 条(明文不离开浏览器)`,
      );
    } catch (e) {
      this._rsaFlash("✗ 加密失败: " + (e && e.message));
    } finally {
      this.rsaBusy = false;
    }
  },

  // 3) 导出建租户 curl(带密文信封,绝不带明文凭据/key)。self-contained:
  //    拼成 curl 文本存 inboundCurl,页面 textarea 展示可复制,不依赖外部 curl 框架。
  exportInboundCurl() {
    if (!this.inboundEncrypted) return this._rsaFlash("✗ 先加密");
    const body = {
      name: "inbound-demo",
      config_template: "default",
      owner_id: "<cognito-sub-uuid>",
      client_token: "<unique-token>",
      env_injected_credentials: this.inboundEncrypted,
    };
    const base = (this.apiUrl || "<API_URL>").replace(/\/+$/, "");
    // body 里只有 enc:v1: 密文,无明文凭据、无 key;x-api-key 用占位不落真 key。
    this.inboundCurl =
      `curl -X POST '${base}/tenants' \\\n` +
      `  -H 'x-api-key: <API_KEY>' \\\n` +
      `  -H 'content-type: application/json' \\\n` +
      `  -d '${JSON.stringify(body)}'`;
    this._rsaFlash("✓ 已生成 curl(只含密文;owner_id/api-key 换成真值)");
  },

  clearInbound() {
    this.inboundItems = [];
    this.inboundEncrypted = null;
    this.inboundNewField = "";
    this.inboundNewValue = "";
    this.inboundCurl = "";
  },
};
