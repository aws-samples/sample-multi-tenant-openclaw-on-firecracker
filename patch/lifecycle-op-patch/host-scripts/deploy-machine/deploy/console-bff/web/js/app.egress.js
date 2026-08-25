// Egress whitelist (#577/#594) — guest 出网 default-deny 白名单的控制页。
//
// 这一页的第一目标不是好看,是【让误操作变难、让漏改看得见】。下面每处"多余的"提示
// 都对应一条真实的后端语义,SPEC 见 engineering/progress/577-594-egress-portal-spec-2026-08-25.md
//
// R1 期望态是【全量替换】:省略 allow 就是把已有放行规则清空 → 表单初值必须是"当前生效值",
//    且提交前必须把新增/删除/保留逐条 diff 出来,而不是等响应回来才说。
// R2 pin 是单向的,机队 mode=off 在【写入时刻】赢 —— 不是"永远赢"。之后一次定向写会按
//    时间戳把机器拉回 host 行的 mode,所以文案不能让人以为熔断是终态。
// R3 前端预校验只是体验层:红线端口/前缀下限/SPIRE 8081 的最终判据在服务端 →
//    通过时只能说"已通过本地检查",不能说"校验通过"。
// R4 收敛异步,上界一个 poll(生产 15s)→ 202 不等于已生效,必须轮询 GET。
// R5 链 sha 跨机合法地不同(LiteLLM 的 A 记录),后端已把它排除在 consistent 之外 →
//    不得把逐机 sha 渲染成一致性。
// R7 "当前生效的链规则"必须从【内核】回读(GET /hosts/egress/chain),不是拿期望态在前端
//    求值。两者在 pinned / 定向灰度 / reconcile 失败的机器上会不一致,而那正是要查的时候。
//    生效判据是三元组(链存在 + FORWARD 跳转数 + 规则 sha),只看"链存在"会漏掉
//    "链在但跳转被删"—— 那种状态下链一个包都收不到。
// R8 放行规则的作用域可以是【指定机器】:此时表单初值必须是那几台自己的当前值,不是 fleet
//    单例的值。选中的机器彼此不一致时要显式说出来,因为提交会把它们统一成同一份。
// R9 每次下发都留一个命名版本(没给名字自动 v1/v2/…),可按机器逐台回滚。
//    回滚也是一次下发,同样要二次确认、同样留版本。
// R10 下发前必须把【最终发出去的 method + path + body】原样显示出来。运维签字签的是这个,
//    不是页面上的开关状态。
window.ocEgress = {
  egLoading: false,
  egStatus: null,            // GET /hosts/egress 的原始响应
  egLastPost: null,          // 最近一次 POST 的响应(用来展示那些易漏字段)
  egError: '',
  egPolling: false,
  _egPollTimer: null,
  // 表单:初值在 loadEgress() 里用当前生效值填,不留空表单(R1)
  egForm: {
    mode: 'deny',
    denyRfc1918: true,
    allow: [],
    targets: 'all',
    picked: {},
    // 只在"指定机器"作用域下可用:钉住 = 该机不受后续全量下发管辖(定向豁免的唯一载体)。
    // 全机队 + pinned=true 后端返 400 —— 一次钉死整机队会让所有后续全量彻底失效。
    pinned: false,
    revisionName: '',        // 留空 = 后端自动 v1/v2/…(R9)
  },
  // 打字确认弹窗
  egConfirm: { show: false, kind: '', typed: '', expect: '', lines: [], busy: false, plan: null },

  // ② 直接粘贴 instance_id 加入作用域(不必先在 ③ 里翻页找)
  egPickText: '',

  // ③ 逐机:默认只显示 10 台,另有定点查询(避免 139 台把页面刷满、也避免翻页找机器)
  egHostLimit: 10,
  egHostQuery: '',
  egHostFilterActive: false,

  // ④ 当前生效的链规则:从内核回读
  egChain: null,
  egChainLoading: false,
  egChainTarget: '',
  egChainError: '',

  // ⑤ 版本与回滚
  egRevisions: null,
  egRevLoading: false,
  egRevError: '',
  egRevPicked: {},           // {revName: {instanceId: true}}
  egRevExpanded: '',

  // ---------- 读 ----------
  async loadEgress() {
    if (!this.apiUrl || !this.apiKey) return;
    this.egLoading = true;
    this.egError = '';
    try {
      this.egStatus = await this.api('GET', this.egStatusPath());
      // R1:表单初值 = 当前生效值。空表单会让"提交即清空"变成默认行为。
      // 注意字段位置:期望态在【嵌套的 desired 里】,不是顶层。真实数据实测过 ——
      // 顶层没有 mode / extra_allow,读顶层会让表单永远看起来"没有规则",
      // 于是"提交即清空"反而变成静默默认,正好是 R1 要防的那件事。
      const d = this.egStatus.desired || {};
      this.egForm.mode = d.mode || 'deny';
      if (typeof d.deny_rfc1918 === 'boolean') this.egForm.denyRfc1918 = d.deny_rfc1918;
      this.egSyncAllowFromScope();
    } catch (e) {
      this.egError = String((e && e.message) || e);
    }
    this.egLoading = false;
  },

  // limit 只截断逐机显示,聚合口径(total/converged/outliers)后端保持全机队 ——
  // 截断了显示行却把 total 也缩小,会让运维以为机队只有 10 台且已全绿。
  egStatusPath() {
    const q = [];
    const ids = this.egQueryIds();
    if (ids.length) {
      q.push('instance_ids=' + encodeURIComponent(ids.join(',')));
    } else if (this.egHostLimit) {
      q.push('limit=' + encodeURIComponent(String(this.egHostLimit)));
    }
    return 'hosts/egress' + (q.length ? '?' + q.join('&') : '');
  },

  egQueryIds() {
    return String(this.egHostQuery || '')
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  },

  async egApplyHostQuery() {
    this.egHostFilterActive = this.egQueryIds().length > 0;
    await this.loadEgress();
  },

  async egClearHostQuery() {
    this.egHostQuery = '';
    this.egHostFilterActive = false;
    await this.loadEgress();
  },

  // 后端把没命中的 id 单独报出来,不静默丢 —— 打错一个字符就查了一台不存在的机器,
  // 而"没有异常"和"没查到这台"在页面上必须长得不一样。
  egQueryNotFound() {
    return (this.egStatus && this.egStatus.instance_ids_not_found) || [];
  },

  egTruncatedNote() {
    const s = this.egStatus || {};
    if (!s.hosts_truncated) return '';
    return `只显示了 ${s.hosts_returned} / ${s.total} 台(limit=${s.limit})—— ` +
           '上面的收敛计数与离群清单仍是全机队口径,没被截断。';
  },

  // 当前生效的放行规则,解析成 [{proto,dport,dst}]。后端存的是 "proto:dport:dst" 逗号分隔。
  egParseHoles(raw) {
    return String(raw || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((tok) => {
        const parts = tok.split(':');
        return { proto: parts[0] || '', dport: parts[1] || '', dst: parts.slice(2).join(':') };
      });
  },

  egFleetHoles() {
    return this.egParseHoles(
      this.egStatus && this.egStatus.desired && this.egStatus.desired.extra_allow,
    );
  },

  // R8:作用域是"指定机器"时,当前值取那几台自己的。后端逐机 report 里带
  // desired_extra_allow(GET 已返回);拿不到就退回 fleet 值并在 UI 上说明。
  egScopeHoleSets() {
    if (this.egTargetsAll()) {
      return [{ id: '__fleet__', holes: this.egFleetHoles(), source: 'fleet' }];
    }
    const byId = {};
    for (const h of this.egHosts()) byId[h.instance_id] = h;
    return this.egPickedIds().map((id) => {
      const h = byId[id];
      const raw = h && (h.desired_extra_allow !== undefined && h.desired_extra_allow !== null)
        ? h.desired_extra_allow
        : null;
      if (raw === null) {
        return { id, holes: this.egFleetHoles(), source: 'fleet-fallback' };
      }
      return { id, holes: this.egParseHoles(raw), source: (h.policy_source || 'host') };
    });
  },

  egTok(h) { return `${h.proto}:${h.dport}:${h.dst}`; },

  // 选中的机器当前值不一致时必须说出来:提交会把它们统一成表单里这一份,
  // 那对其中一部分机器是"顺手改了没打算改的东西"。
  egScopeDivergent() {
    const sets = this.egScopeHoleSets();
    if (sets.length < 2) return null;
    const sigs = sets.map((s) => s.holes.map((h) => this.egTok(h)).sort().join('|'));
    const uniq = Array.from(new Set(sigs));
    if (uniq.length <= 1) return null;
    return sets.map((s, i) => ({ id: s.id, holes: s.holes.map((h) => this.egTok(h)), sig: sigs[i] }));
  },

  // 表单初值:作用域内第一台的当前值(不一致时另有 egScopeDivergent 告警兜住)。
  egSyncAllowFromScope() {
    const sets = this.egScopeHoleSets();
    const base = sets.length ? sets[0].holes : [];
    this.egForm.allow = base.map((h) => ({ ...h }));
  },

  async egSetTargets(kind) {
    this.egForm.targets = kind;
    this.egSyncAllowFromScope();
  },

  egTogglePick(id) {
    this.egForm.picked[id] = !this.egForm.picked[id];
    if (!this.egTargetsAll()) this.egSyncAllowFromScope();
  },

  // 粘贴一串 id 直接加入作用域。不清掉已勾选的,只做并集 —— 分两次粘贴不该互相覆盖。
  egApplyPickText() {
    const ids = String(this.egPickText || '')
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    for (const id of ids) this.egForm.picked[id] = true;
    this.egPickText = '';
    if (ids.length) this.egForm.targets = 'picked';
    this.egSyncAllowFromScope();
  },

  egHosts() { return (this.egStatus && this.egStatus.hosts) || []; },

  // 「从未设置过」是一个独立状态,不能和「设置成 off」混为一谈。
  // 真实数据实测(apse1):fleet 单例从未写过时,服务端只回
  // desired = {mode: null, source: "per-host"} 外加 message="fleet policy has never been set",
  // 于是全机队 converged=0/17、逐机全是"等待收敛"—— 看起来像机队卡住了,其实是压根没配过。
  egNeverConfigured() {
    const s = this.egStatus;
    if (!s) return false;
    const d = s.desired || {};
    return d.source === 'per-host' || !d.mode;
  },

  // reason 不在逐机 report 上,而在【独立的 outliers 列表】里({instance_id, reason})。
  // 直接读 h.reason 会恒为 undefined。
  egReason(h) {
    const out = (this.egStatus && this.egStatus.outliers) || [];
    const hit = out.find((o) => o.instance_id === (h && h.instance_id));
    return hit ? hit.reason : '';
  },

  // ---------- 逐机三态(不合成一个"健康"灯) ----------
  egHostState(h) {
    if (!h) return { key: 'unknown', label: '未知', cls: 'eg-state-unknown' };
    if (h.pinned_malformed) {
      return { key: 'malformed', label: 'pin 类型不对', cls: 'eg-state-bad',
               hint: 'egress_pinned 存在但不是 DDB BOOL —— 运维以为豁免了其实没有' };
    }
    if (h.pinned) {
      return { key: 'exempt', label: '豁免中', cls: 'eg-state-exempt',
               hint: '不受机队策略管辖(机队熔断 mode=off 仍会在写入时刻拆它的链)' };
    }
    // 服务端在 report 里把 DDB 的 egress_* 前缀剥掉了(applied_mode / reconcile_error /
    // applied_version / applied_sha256)。读 DDB 的属性名会恒为 undefined —— 那样"收敛失败"
    // 这一态永远不会出现,页面看着全绿。真实数据实测过。
    if (h.reconcile_error) {
      return { key: 'error', label: '收敛失败', cls: 'eg-state-bad', hint: h.reconcile_error };
    }
    if (h.converged) return { key: 'ok', label: '已收敛', cls: 'eg-state-ok' };
    // R4:版本不等时默认按"等待收敛"呈现,不当错误 —— 上界一个 poll。
    return { key: 'pending', label: '等待收敛', cls: 'eg-state-pending',
             hint: '收敛是异步的,上界约一个 poll(生产 15s)' };
  },

  // ---------- R3 本地预校验(只提示,不判定) ----------
  EG_REDLINE_PORTS: [22, 3306, 5432, 6379, 6380, 8877, 8899, 9090, 9100,
                     9200, 9300, 11211, 18789, 27017],
  EG_MIN_PREFIX: 24,

  egCheckHole(h) {
    if (!h) return '';
    const port = Number(h.dport);
    if (!Number.isInteger(port) || port < 1 || port > 65535) return 'dport 必须是 1-65535';
    if (!['tcp', 'udp'].includes(String(h.proto).toLowerCase())) return 'proto 只能是 tcp/udp';
    const dst = String(h.dst || '').trim();
    if (!dst) return 'dst 必填(IP 或 CIDR)';
    if (dst.includes(':')) return 'dst 必须是 IPv4 —— 链是 IPv4-only';
    if (this.EG_REDLINE_PORTS.includes(port)) return `dport ${port} 是红线端口`;
    if (dst.startsWith('169.254.169.254')) return '不得对 IMDS 开洞';
    const slash = dst.indexOf('/');
    if (slash >= 0) {
      const p = Number(dst.slice(slash + 1));
      if (!Number.isInteger(p) || p < 0 || p > 32) return '前缀长度必须是 0-32';
      if (p < this.EG_MIN_PREFIX) return `前缀不得宽于 /${this.EG_MIN_PREFIX}`;
    }
    if (port === 8081) return 'SPIRE 8081 只允许 SPIRE server 的 /32,由服务端裁决';
    return '';
  },

  egLocalCheck() {
    const errs = this.egForm.allow.map((h) => this.egCheckHole(h)).filter(Boolean);
    return { ok: errs.length === 0, errs };
  },

  // R3:通过时只能说"已通过本地检查",不能说"校验通过"。
  egLocalCheckLabel() {
    const r = this.egLocalCheck();
    if (!r.ok) return `本地检查未通过:${r.errs[0]}`;
    return '已通过本地检查,最终由服务端裁决';
  },

  egAddHole() { this.egForm.allow.push({ proto: 'tcp', dport: '', dst: '' }); },
  egDelHole(i) { this.egForm.allow.splice(i, 1); },
  // 「恢复成当前生效值」:改坏了要能一键回到起点,而不是靠刷新页面(刷新会丢掉作用域选择)。
  egResetHoles() { this.egSyncAllowFromScope(); },

  // ---------- R1 发送前 diff ----------
  // 逐条算 新增 / 删除 / 保留。只报"清空了什么"不够 —— 运维要看的是"这次提交把
  // 现状变成了什么",少一条多一条都得肉眼可见。
  egHoleDiff() {
    const sets = this.egScopeHoleSets();
    const before = sets.length ? sets[0].holes.map((h) => this.egTok(h)) : [];
    const after = this.egForm.allow.map((h) => this.egTok(h));
    const bs = new Set(before);
    const as = new Set(after);
    return {
      added: after.filter((t) => !bs.has(t)),
      removed: before.filter((t) => !as.has(t)),
      kept: after.filter((t) => bs.has(t)),
      beforeCount: before.length,
      afterCount: after.length,
      scope: sets.length && sets[0].id === '__fleet__' ? 'fleet 单例' : `${sets.length} 台机器`,
    };
  },

  egHasHoleChange() {
    const d = this.egHoleDiff();
    return d.added.length > 0 || d.removed.length > 0;
  },

  egClearedByThisSubmit() { return this.egHoleDiff().removed; },

  // ---------- 危险操作的确认流 ----------
  egTargetsAll() { return this.egForm.targets === 'all'; },
  egPickedIds() { return Object.keys(this.egForm.picked).filter((k) => this.egForm.picked[k]); },

  // targets=all + pinned=true 后端返 400:按钮直接不可点,并说明原因。
  egPinAllBlocked() { return this.egTargetsAll(); },
  egPinAllBlockedWhy() {
    return '一次钉死整机队会让后续所有全量下发彻底失效,后端会返 400。逐台或分批做。';
  },

  // R10 —— 最终发出去的请求原样显示。运维签字签的是这个,不是页面上的开关状态。
  egPlanFor(kind) {
    if (kind === 'unpin-all') {
      return { method: 'POST', path: 'hosts/egress',
               body: { targets: 'all', pinned: false, mode: this.egForm.mode,
                       revision_name: this.egForm.revisionName || undefined } };
    }
    if (kind === 'fleet-off') {
      return { method: 'POST', path: 'hosts/egress',
               body: { targets: 'all', mode: 'off',
                       revision_name: this.egForm.revisionName || undefined } };
    }
    return { method: 'POST', path: 'hosts/egress', body: this.egBuildBody() };
  },

  egPlanJson(plan) {
    if (!plan) return '';
    const b = JSON.parse(JSON.stringify(plan.body || {}));
    return `${plan.method} ${String(this.apiUrl || '').replace(/\/+$/, '')}/${plan.path}\n` +
           `x-api-key: ****\n\n` + JSON.stringify(b, null, 2);
  },

  egAskConfirm(kind) {
    const diff = this.egHoleDiff();
    const n = this.egTargetsAll() ? (this.egStatus && this.egStatus.total) || this.egHosts().length
                                  : this.egPickedIds().length;
    const pinnedNow = this.egHosts().filter((h) => h.pinned).map((h) => h.instance_id);
    const lines = [];
    if (kind === 'fleet-off') {
      lines.push(`这会拆掉 ${n} 台 host 的链 —— 包括 pin 住的 ${pinnedNow.length} 台。`);
      lines.push('机队熔断在【写入时刻】赢:pin 挡不住它。但这不是永久钉死 —— 之后一次定向写' +
                 '会按时间戳把那台机器拉回它 host 行的 mode。');
      lines.push('拆链后 guest 出网回到不受本链管控的状态(per-tap 黑名单层仍在)。');
    } else if (kind === 'fleet-enable') {
      lines.push(this.egTargetsAll()
        ? `这会给 ${n} 台 host 装上 default-deny 白名单链。`
        : `这只发给选中的 ${n} 台:${this.egPickedIds().join(', ')}`);
      lines.push('配错一条放行规则 = 该机全部租户的出网中断,所以这一步要打字确认。');
      if (this.egTargetsAll() && pinnedNow.length) {
        lines.push(`会被跳过(pinned_skipped)的:${pinnedNow.join(', ')}`);
      }
      lines.push('收敛是异步的,上界约一个 poll(生产 15s);202 不等于已生效。');
    } else if (kind === 'unpin-all') {
      lines.push(`这会解除所有 host 的豁免,让它们重新被全量接管(当前 pinned:${pinnedNow.length} 台)。`);
      lines.push('提交后请核对响应里的 unpinned_count 与 unpin_failed —— 部分失败时' +
                 '那些机器仍带 pin,会继续否决后续全量。');
    }
    if (diff.removed.length) {
      lines.push(`⚠ 期望态是全量替换:本次提交会【删掉】这些已生效的放行规则 —— ${diff.removed.join(' / ')}`);
    }
    if (diff.added.length) {
      lines.push(`本次新增:${diff.added.join(' / ')}`);
    }
    if (!this.egTargetsAll() && this.egForm.pinned) {
      lines.push('这几台会被【钉住】:之后的全量下发会跳过它们(pinned_skipped),' +
                 '只有机队熔断能穿透。忘了解除的话,例行策略更新对它们无效且不报错。');
    }
    const div = this.egScopeDivergent();
    if (div) {
      lines.push('⚠ 选中的机器当前放行规则【彼此不一致】,提交会把它们统一成上面这一份 —— ' +
                 div.map((d) => `${d.id}: ${d.holes.join(' / ') || '(无)'}`).join(' ; '));
    }
    lines.push(`版本名:${this.egForm.revisionName || '(留空 → 后端自动 v1/v2/…)'};` +
               '下发前会记录改动前的期望态,之后可按机器逐台回滚。');
    this.egConfirm = {
      show: true, kind, typed: '', busy: false, lines,
      plan: this.egPlanFor(kind),
      expect: (kind === 'fleet-off' || kind === 'fleet-enable') ? 'CONFIRM' : '',
    };
  },

  egConfirmReady() {
    const c = this.egConfirm;
    if (!c.expect) return true;
    return c.typed.trim() === c.expect;
  },

  // ---------- 写 ----------
  async egSubmitConfirmed() {
    const c = this.egConfirm;
    if (!this.egConfirmReady()) return;
    c.busy = true;
    try {
      await this.egPostTo(c.plan.path, c.plan.body);
      this.egConfirm.show = false;
    } finally {
      c.busy = false;
    }
  },

  egBuildBody() {
    const body = {
      mode: this.egForm.mode,
      deny_rfc1918: !!this.egForm.denyRfc1918,
      targets: this.egTargetsAll() ? 'all' : this.egPickedIds(),
    };
    // R1:显式带上 allow —— 即使是空数组也要带,让"清空"是一个明确的意图而不是遗漏。
    body.allow = this.egForm.allow.map((h) => ({
      proto: String(h.proto).toLowerCase(), dport: Number(h.dport), dst: String(h.dst).trim(),
    }));
    // 只在定向作用域带 pinned:全机队带 true 后端会 400,带 false 又会顺手清掉别人的豁免。
    if (!this.egTargetsAll() && this.egForm.pinned) body.pinned = true;
    if (this.egForm.revisionName) body.revision_name = this.egForm.revisionName;
    return body;
  },

  async egPost(body) { return this.egPostTo('hosts/egress', body); },

  async egPostTo(path, body) {
    this.egError = '';
    try {
      this.egLastPost = await this.api('POST', path, body);
      // R4:下发被接受 ≠ 已生效。进入轮询,由 GET 决定何时显示收敛。
      this.egStartPolling();
      this.egLoadRevisions();
    } catch (e) {
      this.egError = String((e && e.message) || e);
    }
  },

  // ---------- R4 轮询 ----------
  egStartPolling() {
    this.egStopPolling();
    this.egPolling = true;
    let rounds = 0;
    const tick = async () => {
      rounds += 1;
      await this.loadEgress();
      // 收敛了或者轮询超过 8 次(约 2 分钟)就停,避免无限轮询
      if ((this.egStatus && this.egStatus.fully_converged) || rounds >= 8) {
        this.egStopPolling();
        return;
      }
      this._egPollTimer = setTimeout(tick, 15000);
    };
    this._egPollTimer = setTimeout(tick, 3000);
  },

  egStopPolling() {
    if (this._egPollTimer) { clearTimeout(this._egPollTimer); this._egPollTimer = null; }
    this.egPolling = false;
  },

  // ---------- R7 当前生效的链规则:从内核回读 ----------
  async egLoadChain(instanceId) {
    const id = String(instanceId || this.egChainTarget || '').trim();
    if (!id) { this.egChainError = '先选一台机器'; return; }
    this.egChainTarget = id;
    this.egChainLoading = true;
    this.egChainError = '';
    try {
      this.egChain = await this.api('GET', 'hosts/egress/chain?instance_id=' + encodeURIComponent(id));
    } catch (e) {
      this.egChain = null;
      this.egChainError = String((e && e.message) || e);
    }
    this.egChainLoading = false;
  },

  // 三元组判据。后端已经算好 verdict,前端不重算 —— 但要把三个分量都摊开显示,
  // 否则 NOT_EFFECTIVE 时运维不知道是"链没了"还是"跳转没了"。
  egChainVerdict() {
    const c = this.egChain;
    if (!c) return null;
    return {
      verdict: c.verdict || 'INCONCLUSIVE',
      present: c.chain_present === true,
      jumps: typeof c.forward_jumps === 'number' ? c.forward_jumps : null,
      sha: c.rules_sha256 || '',
      readAt: c.read_at || '',
      error: c.error || '',
    };
  },

  egChainRules() { return (this.egChain && this.egChain.rules) || []; },
  egChainPerTap() { return (this.egChain && this.egChain.per_tap_sample) || []; },

  egChainCaveat() {
    return '这是从该机内核回读的共享链一层。内核实际叠三层(per-tap 黑名单 + 本链 + ' +
           'INPUT 方向),链 sha 跨机合法地不同(LiteLLM 的 A 记录),不要把它当一致性判据。' +
           '"读不到"(INCONCLUSIVE)与"没装链"(NOT_EFFECTIVE)是两件事。';
  },

  // ---------- R9 版本与逐台回滚 ----------
  async egLoadRevisions() {
    this.egRevLoading = true;
    this.egRevError = '';
    try {
      this.egRevisions = await this.api('GET', 'hosts/egress/revisions');
    } catch (e) {
      this.egRevError = String((e && e.message) || e);
    }
    this.egRevLoading = false;
  },

  egRevList() { return (this.egRevisions && this.egRevisions.revisions) || []; },

  // before_incomplete = 记录这条版本时【读不到】改动前的期望态(只有 break-glass 熔断会
  // 走到那里:DDB 挂了也不能挡住熔断)。它是一笔变更账,不是回滚点 —— 后端回滚会 409。
  // 前端必须在列表里标出来并禁掉按钮:不标的话它与真锚点长得一模一样,运维会先点一次
  // 再去查那个 409 是什么意思。
  egRevUsable(rev) { return !(rev && rev.before_incomplete === true); },
  egRevUnusableWhy() {
    return '这条版本记录时读不到改动前的期望态(break-glass 熔断路径),不能当回滚点用 —— ' +
           '拿它回滚等于按一份没读到的旧态去写。改用它之前的某个完整版本。';
  },
  egNextAutoName() { return (this.egRevisions && this.egRevisions.next_auto_name) || 'v1'; },

  egRevHosts(rev) {
    const before = (rev && rev.before) || {};
    return Object.keys(before).filter((k) => k !== '__fleet__').sort();
  },

  egRevBeforeText(rev, id) {
    const b = ((rev && rev.before) || {})[id];
    if (!b) return '(无记录)';
    const holes = b.extra_allow ? b.extra_allow : '(无放行规则)';
    return `mode=${b.mode || '(未设置)'} · deny_rfc1918=${b.deny_rfc1918 ? '是' : '否'} · ` +
           `${holes} · 来源=${b.source}${b.pinned ? ' · 豁免中' : ''}`;
  },

  egRevToggle(revName, id) {
    if (!this.egRevPicked[revName]) this.egRevPicked[revName] = {};
    this.egRevPicked[revName][id] = !this.egRevPicked[revName][id];
  },

  egRevPickedIds(revName) {
    const m = this.egRevPicked[revName] || {};
    return Object.keys(m).filter((k) => m[k]);
  },

  egRevPickAll(rev) {
    this.egRevPicked[rev.name] = {};
    for (const id of this.egRevHosts(rev)) this.egRevPicked[rev.name][id] = true;
  },

  // 回滚也是一次下发:同样二次确认、同样显示最终参数、同样留新版本。
  egAskRollback(rev, allHosts) {
    if (!this.egRevUsable(rev)) { this.egError = this.egRevUnusableWhy(); return; }
    const ids = allHosts ? this.egRevHosts(rev) : this.egRevPickedIds(rev.name);
    if (!ids.length) { this.egError = '先勾选要回滚的机器'; return; }
    const lines = [
      `回滚到版本 ${rev.name}(${rev.created_at || '时间未记录'},作用域 ${rev.scope})。`,
      `只影响勾选的 ${ids.length} 台:${ids.join(', ')}`,
      '逐台恢复该版本记录的【改动前】期望态:',
    ];
    for (const id of ids) lines.push(`  · ${id} → ${this.egRevBeforeText(rev, id)}`);
    const fromFleet = ids.filter((id) => (((rev.before || {})[id] || {}).source) === 'fleet');
    if (fromFleet.length) {
      lines.push(`⚠ 这 ${fromFleet.length} 台当时是跟着 fleet 单例走的。回滚会把当时那份取值` +
                 '【写成它自己的 host 行】,不是让它重新跟 fleet —— 因为 fleet 单例可能已经变了,' +
                 '跟过去等于回滚到另一份规则。回滚后它们会带显式 host 行。');
    }
    const wasNone = ids.filter((id) => (((rev.before || {})[id] || {}).source) === 'none');
    if (wasNone.length) {
      lines.push(`⚠ 这 ${wasNone.length} 台当时【没有任何期望态】。host-agent 会把缺失强制成 off,` +
                 '所以复现当时的观测态 = mode=off = 拆链。');
    }
    lines.push('回滚本身也会留一个新版本,可以再回滚回来。');
    this.egConfirm = {
      show: true, kind: 'rollback', typed: '', busy: false, lines,
      expect: 'CONFIRM',
      plan: {
        method: 'POST', path: 'hosts/egress/rollback',
        body: {
          revision: rev.name,
          targets: allHosts ? 'all' : ids,
          revision_name: this.egForm.revisionName || undefined,
        },
      },
    };
  },

  // ---------- 必须上屏的告警(漏一个就等于没修) ----------
  egAlerts() {
    const out = [];
    const p = this.egLastPost || {};
    const s = this.egStatus || {};
    if (this.egNeverConfigured()) {
      out.push({ level: 'warn', text:
        '机队的 fleet egress 策略【从未设置过】(desired.source=per-host)—— 所以下面逐机' +
        '全是"等待收敛"、converged 为 0,这不是机队卡住,是压根没配过。' +
        '「从未设置」与「设置成 off」是两回事:前者链从来没装,后者是显式关掉。' +
        (s.message ? ` 服务端原话:${s.message}` : '') });
    }
    if (p.revision && p.revision.error) {
      out.push({ level: 'bad', text:
        `这次下发【没能留下回滚点】:${p.revision.error} —— 只有 break-glass(mode=off)` +
        '才允许在记不下版本时继续下发。这台机队现在改回去只能手工重建期望态。' });
    } else if (p.revision && p.revision.name) {
      out.push({ level: 'info', text:
        `已记录版本 ${p.revision.name}${p.revision.auto_named ? '(自动命名)' : ''},` +
        `覆盖 ${p.revision.hosts_recorded} 台的改动前期望态 —— 可从下面「版本与回滚」逐台回滚。` });
    }
    if (p.not_in_revision && p.not_in_revision.length) {
      out.push({ level: 'warn', text:
        `这些机器不在该版本的记录里,本次回滚【没有发给它们】:${p.not_in_revision.join(', ')}` });
    }
    if (p.failed_dispatches && p.failed_dispatches.length) {
      out.push({ level: 'bad', text:
        `部分下发失败(${p.failed_dispatches.length} 组)—— 已成功的组仍然生效,机队现在是` +
        '混合状态。逐组核对 dispatches / failed_dispatches 再决定重发范围。' });
    }
    if (p.pin_check_unavailable && p.pin_check_unavailable.length) {
      out.push({ level: 'bad', text:
        `pin 自检不可用:${p.pin_check_unavailable.join(', ')} —— 这几台读 DDB 失败、` +
        '按"未 pin"继续执行了。从外面看与"机队里本来就没有 pinned 机器"不可区分。' });
    }
    if (p.unpin_failed && p.unpin_failed.length) {
      out.push({ level: 'bad', text:
        `批量解除【部分失败】:${p.unpin_failed.join(', ')} —— 这些机器仍带 pin,` +
        '会继续否决后续所有例行全量下发。' });
    }
    if (p.extra_allow_cleared) {
      out.push({ level: 'warn', text:
        `本次提交清空了放行规则(期望态是全量替换):${JSON.stringify(p.extra_allow_cleared)}` });
    }
    if (p.pinned_skipped && p.pinned_skipped.length) {
      out.push({ level: 'info', text: `本次全量跳过了(pinned_skipped):${p.pinned_skipped.join(', ')}` });
    }
    if (p.pinned_torn_down && p.pinned_torn_down.length) {
      out.push({ level: 'warn', text:
        `熔断拆掉了这些 pin 住机器的链(pinned_torn_down):${p.pinned_torn_down.join(', ')}` });
    }
    if (typeof p.unpinned_count === 'number' && p.unpinned_count > 0) {
      out.push({ level: 'info', text: `解除豁免 ${p.unpinned_count} 台` });
    }
    if (s.pinned_malformed_count) {
      out.push({ level: 'bad', text:
        `${s.pinned_malformed_count} 台的 egress_pinned 类型不对(不是 DDB BOOL)—— ` +
        '这些机器的运维以为豁免了,实际没有。' });
    }
    if (this.egQueryNotFound().length) {
      out.push({ level: 'warn', text:
        `定点查询里这些 instance_id 在机队里查不到:${this.egQueryNotFound().join(', ')} —— ` +
        '"没有异常"和"没查到这台"是两件事。' });
    }
    // 两个正交布尔:只看 fully_converged 会让"机队跑着两份策略"报成全绿。
    if (s.fully_converged && s.fleet_uniform === false) {
      out.push({ level: 'info', text:
        '机队正跑着两份策略(有豁免机器):fully_converged=✓ 但 fleet_uniform=✗。' +
        '这不是故障,是有人做了定向豁免。' });
    }
    return out;
  },
};
