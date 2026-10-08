// Jable STRM 控制台（Alpine.js）

const KIND_NAMES = {
  list: "列表页", detail: "详情页", rewrite: "重写输出", purge: "删除库", reclassify: "重新归库",
  scan: "扫描", adopt: "纳管", prefix: "改前缀", revert: "回滚", locate: "同步位置",
  crawl: "列表抓取", incremental: "增量", videos: "指定影片", probe: "补源", verify: "核对输出", cover: "补封面",
};
const JOB_STATUS = { running: "运行中", paused: "已暂停", done: "已完成", cancelled: "已取消" };
const TASK_STATUS = { pending: "待处理", running: "运行中", done: "完成", failed: "失败", gone: "下架", cancelled: "已取消" };
const STRM_KINDS = { ours: "本服务格式", cdn: "CDN 直链", named: "文件名识别", other: "其他来源", invalid: "无效" };
const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

const SETTING_GROUPS = [
  { title: "站点", keys: ["sites", "site_priority"] },
  { title: "抓取", keys: ["proxy", "impersonate", "request_timeout", "domain_cooldown", "solver_url", "solver_timeout"] },
  { title: "重试", keys: ["max_attempts", "retry_base_delay"] },
  { title: "任务", keys: ["fetch_detail", "auto_probe_sites", "probe_recheck_days", "external_restore"] },
  { title: "输出", keys: ["output_dir", "path_template", "write_nfo", "download_cover", "poster_crop"] },
  { title: "播放", keys: ["public_base_url", "play_mode", "proxy_user_agents", "play_token", "hls_margin", "subtitle_priority",
                         "subtitle_fallback", "play_discover", "resolve_timeout", "resolve_token", "resolve_proxy_url"] },
];
const SETTING_LABELS = {
  sites: "站点", site_priority: "站点优先顺序", proxy: "抓取代理", impersonate: "浏览器指纹",
  request_timeout: "请求超时", domain_cooldown: "域名冷却", solver_url: "解题服务地址",
  solver_timeout: "解题超时", max_attempts: "最大尝试次数", retry_base_delay: "首次重试间隔",
  fetch_detail: "默认抓取详情", output_dir: "输出根目录", path_template: "默认路径模板", write_nfo: "写 nfo",
  download_cover: "下载封面", poster_crop: "裁剪 poster", public_base_url: "对外地址", play_mode: "播放模式",
  proxy_user_agents: "中转 UA 片段", play_token: "播放令牌", hls_margin: "有效期余量（分钟）",
  resolve_token: "网关解析令牌", subtitle_priority: "字幕偏好", subtitle_fallback: "字幕回退",
  resolve_timeout: "取地址总时限（秒）", resolve_proxy_url: "公网中转地址", play_discover: "现场找源",
  auto_probe_sites: "新片自动补源", probe_recheck_days: "补源重查间隔（天）", external_restore: "外部整理库补回丢失的 strm",
};
const SUBTITLES = { zh: "中文字幕", en: "英文字幕", "": "无字幕" };
const REWRITE_KEYS = ["public_base_url", "play_mode", "play_token", "path_template", "output_dir", "write_nfo"];
const PLAY_MODES = { redirect: "302 跳转（ffmpeg 类客户端自动中转）", proxy: "全部中转", direct: "直写 CDN 地址（仅调试）" };

function app() {
  return {
    tabs: [
      { id: "overview", name: "概览" }, { id: "jobs", name: "任务" }, { id: "videos", name: "影片库" },
      { id: "libraries", name: "输出库与订阅" }, { id: "strm", name: "strm 管理" },
      { id: "settings", name: "设置" }, { id: "logs", name: "日志" },
    ],
    tab: "overview",
    s: {},
    offline: false,
    testing: false,
    testResult: null,
    meta: { sites: {} },
    form: { kind: "list", site: "jable", source: "", sort: "post_date", start_page: 1, end_page: 0, detail: true, urls: "", library_id: 1,
            repair: true, covers: true, force_external: false },
    libraries: [],
    subscriptions: [],
    libForm: { rule: {} },
    facets: {},
    subForm: {},
    strmKinds: STRM_KINDS,
    scans: [],
    scanId: null,
    scanDir: "",
    scanSummary: null,
    sf: { kind: "", managed: "", prefix: "", q: "", page: 1, size: 50 },
    strmFiles: { items: [], total: 0 },
    showMissing: false,
    missing: { items: [], total: 0 },
    adoptForm: { library_id: 0, kinds: ["ours", "cdn"], fetch_missing: true, prefix: "" },
    prefixForm: { old: "", new: "", preview: null },
    changeSets: [],
    jobs: [],
    openJobId: null,
    taskFilter: "failed",
    tasks: [],
    vq: { q: "", filter: "", page: 1, size: 50, library_id: 0 },
    videos: { items: [], total: 0 },
    settings: {},
    draft: {},
    needRewrite: false,
    solverTest: { busy: false, mode: "", result: null },
    solverSite: "jable",
    settingGroups: SETTING_GROUPS,
    labels: SETTING_LABELS,
    logs: [],
    logLevel: "INFO",
    logFilter: "",
    logFollow: true,
    logConnected: false,
    detail: null,
    player: null,
    hls: null,
    toast: null,

    async init() {
      const fromHash = location.hash.slice(1);
      if (this.tabs.some(t => t.id === fromHash)) this.tab = fromHash;
      window.addEventListener("hashchange", () => {
        const h = location.hash.slice(1);
        if (this.tabs.some(t => t.id === h)) this.go(h);
      });
      this.meta = await this.get("/api/meta").catch(() => this.meta);
      this.resetLibForm();
      this.resetSubForm();
      this.loadLibraries();
      this.refreshStatus();
      this.onTab();
      this.connectLogs();
      setInterval(() => this.tick(), 3000);
    },

    go(id) {
      this.tab = id;
      if (location.hash.slice(1) !== id) history.replaceState(null, "", "#" + id);
      this.onTab();
    },
    onTab() {
      if (this.tab === "jobs") { this.loadJobs(); this.loadLibraries(); }
      if (this.tab === "videos") { this.loadVideos(); this.loadLibraries(); }
      if (this.tab === "libraries") { this.loadLibraries(); this.loadSubs(); this.loadFacets(); }
      if (this.tab === "strm") { this.loadLibraries(); this.loadScans(); this.loadChangeSets(); }
      if (this.tab === "settings") this.loadSettings();
      if (this.tab === "logs") this.$nextTick(() => this.scrollLogs(true));
    },
    tick() {
      if (document.hidden) return;
      this.refreshStatus();
      if (this.tab === "jobs") { this.loadJobs(); if (this.openJobId) this.loadTasks(); }
      if (this.tab === "libraries") { this.loadLibraries(); this.loadSubs(); }
      if (this.tab === "strm" && this.scans.some(j => j.status === "running")) this.loadScans();
    },

    // ---- HTTP ----
    async req(method, url, body) {
      const opt = { method, headers: {} };
      if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
      const r = await fetch(url, opt);
      const text = await r.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch { data = text; }
      if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
      return data;
    },
    get(url) { return this.req("GET", url); },
    post(url, body) { return this.req("POST", url, body ?? {}).catch(e => { this.notify(e.message, true); throw e; }); },
    notify(msg, err = false) {
      this.toast = { msg, err };
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => (this.toast = null), err ? 6000 : 2500);
    },

    // ---- 概览 ----
    async refreshStatus() {
      try { this.s = await this.get("/api/status"); this.offline = false; }
      catch { this.offline = true; }
    },
    siteMeta(name) { return this.meta.sites?.[name] || { label: name, presets: [], sorts: {} }; },
    siteLabel(name) { return this.siteMeta(name).label || name || ""; },
    siteTitle(st) { return st.label + (st.enabled ? "" : "（未启用）") + (st.blocked_for > 0 ? "，被拦截 " + this.fmtDur(st.blocked_for) : ""); },
    blockedText() {
      return Object.entries(this.s.engine?.blocked || {}).map(([k, v]) => `${this.siteLabel(k)} 被拦截（${this.fmtDur(v)} 后重试）`).join("，");
    },
    rateText() {
      return (this.s.sites || []).filter(x => x.enabled).map(x => `${x.label} ${x.rate.current}/${x.rate.limit}`).join(" · ") || "-";
    },
    domainRows() {
      return (this.s.sites || []).flatMap(st => st.domains.map((d, i) => ({ key: st.name + d.base, site: st, d, first: i === 0 })));
    },
    engineState() {
      if (this.offline) return { text: "服务离线", cls: "err" };
      const e = this.s.engine;
      if (!e) return { text: "加载中", cls: "" };
      if (e.paused) return { text: "已暂停", cls: "" };
      if (e.blocked_for > 0) return { text: Object.keys(e.blocked).map(k => this.siteLabel(k)).join("、") + " 被拦截", cls: "warn" };
      if (e.running.length) return { text: "运行中", cls: "ok" };
      return { text: "空闲", cls: "info" };
    },
    c(key) { return this.s.metrics?.counters?.[key] || 0; },
    queueSum(st) { return Object.values(this.s.queue || {}).reduce((a, q) => a + (q[st] || 0), 0); },
    async testDomains() {
      this.testing = true;
      try { this.testResult = await this.post("/api/fetcher/test"); this.refreshStatus(); }
      finally { this.testing = false; }
    },
    async quickJob(kind, extra = {}) {
      if (kind === "rewrite" && !confirm("按当前设置重写全部 strm / nfo？")) return;
      const r = await this.post("/api/jobs", { kind, ...extra });
      this.notify(`已创建任务 #${r.id}`);
      this.refreshStatus();
      if (this.tab === "jobs") this.loadJobs();
    },

    // ---- 任务 ----
    async loadJobs() { this.jobs = await this.get("/api/jobs").catch(() => this.jobs); },
    jobHint() {
      return {
        list: `一次性抓取某个列表并输出到所选的库。${this.siteMeta(this.form.site).hint || ""}。同一番号已经在库里（别的站抓过）的，只给它加一个源。需要定时更新请用「输出库与订阅」里的订阅。`,
        videos: "抓取指定影片的详情并加入所选的库，优先级高于批量任务。",
        backfill: "为所有还没有详情的影片排队抓详情，写入它们所在的各个库。",
        verify: "检查数据库里每条输出在磁盘上还在不在：strm 有没有、内容是不是当前的播放地址，nfo 和封面有没有。勾上「补回」会重新写 strm 和 nfo（不联网），封面先从别的库硬链接，没有再下载。外部整理库按 strm 内容在收件目录和外部整理目录里找（外部工具改了目录、加了 -C / -破解 之类的后缀也认得出），找到就只更新记录的路径；两边都找不到才写回收件目录，交给外部工具再整理；外部整理目录不在或是空的时候不补（多半是挂载出了问题），确认要补就勾「外部整理目录是空的也写回」。订阅增量遇到文件丢了的影片也会按同样的规则顺手处理。",
        probe: "给库里的影片找备用源：按番号到所选站点逐部查找（每部一次请求），找到就挂成这部影片的另一个源，播放时原来的源不能用会自动换过去。某个站没有的影片，按「补源重查间隔」内不再重复查。",
        rewrite: "修改对外地址、播放模式、令牌或路径模板后，用它重写已有的 strm / nfo（不联网），路径变了会搬动文件。",
      }[this.form.kind];
    },
    async createJob() {
      const f = this.form;
      const body = { kind: f.kind };
      if (f.kind === "list") Object.assign(body, { site: f.site, source: f.source, sort: f.sort, start_page: f.start_page || 1, end_page: f.end_page || 0, detail: f.detail });
      if (f.kind === "videos") Object.assign(body, { site: f.site, urls: f.urls });
      if (f.kind === "probe") body.site = f.site;
      if (f.kind === "verify") Object.assign(body, { repair: f.repair, covers: f.repair && f.covers, force_external: f.repair && f.force_external });
      if (["list", "videos", "rewrite", "probe", "verify"].includes(f.kind) && f.library_id) body.library_id = f.library_id;
      const r = await this.post("/api/jobs", body);
      this.notify(`已创建任务 #${r.id}`);
      this.loadJobs();
    },
    async jobAction(j, action) {
      if (action === "cancel" && !confirm(`取消任务 #${j.id}？未执行的子任务将不再执行。`)) return;
      await this.post(`/api/jobs/${j.id}/${action}`);
      this.loadJobs();
    },
    async deleteJob(j) {
      if (!confirm(`删除任务 #${j.id} 及其子任务记录？（不影响已入库的影片和已生成的文件）`)) return;
      await this.req("DELETE", `/api/jobs/${j.id}`).catch(e => this.notify(e.message, true));
      if (this.openJobId === j.id) this.openJobId = null;
      this.loadJobs();
    },
    openJob(j) {
      this.openJobId = j.id;
      this.taskFilter = (j.tasks?.failed || 0) > 0 ? "failed" : "";
      this.loadTasks();
    },
    async loadTasks() {
      if (!this.openJobId) return;
      this.tasks = await this.get(`/api/jobs/${this.openJobId}/tasks?status=${this.taskFilter}&limit=200`).catch(() => []);
    },
    jobTotal(j) { return Object.values(j.tasks || {}).reduce((a, b) => a + b, 0); },
    jobFinished(j) { const t = j.tasks || {}; return (t.done || 0) + (t.failed || 0) + (t.gone || 0) + (t.cancelled || 0); },
    jobPct(j) { const total = this.jobTotal(j); return total ? Math.round(this.jobFinished(j) * 100 / total) : 0; },
    jobProgressText(j) {
      let text = `${this.fmtNum(this.jobFinished(j))} / ${this.fmtNum(this.jobTotal(j))}`;
      if (j.state?.last_page) text += ` · 共 ${j.state.last_page} 页`;
      if (j.kind === "locate" && j.state?.checked != null) text += ` · 更新 ${this.fmtNum(j.state.updated)}，找不到 ${this.fmtNum(j.state.missing)}`;
      if (j.kind === "verify" && j.state?.checked != null) {
        const st = j.state, n = k => this.fmtNum(st[k] || 0);
        text += ` · 检查 ${n("checked")}，正常 ${n("ok")}，strm 缺 ${n("strm")}，nfo 缺 ${n("nfo")}，封面缺 ${n("cover")}`
          + (j.params?.repair ? `；补写 ${n("repaired")}，补封面 ${n("covers_queued")}` : "")
          + (st.external_relocated ? `；外部整理库找回位置 ${n("external_relocated")}` : "")
          + (st.external_rewritten ? `，写回收件目录 ${n("external_rewritten")}` : "")
          + (st.external_missing ? `，找不到 ${n("external_missing")}` : "")
          + (st.external_unavailable ? `，外部整理目录不在或是空的没补 ${n("external_unavailable")}` : "");
      }
      return text;
    },
    jobCls(st) { return { running: "ok", paused: "warn", done: "info", cancelled: "" }[st] || ""; },
    taskCls(st) { return { done: "ok", failed: "err", running: "warn", gone: "" }[st] || ""; },
    jobStatusName(st) { return JOB_STATUS[st] || st; },
    taskStatusName(st) { return TASK_STATUS[st] || st; },
    kindName(k) { return KIND_NAMES[k] || k; },
    errorHtml(msg) {
      if (!msg) return "";
      const esc = msg.replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
      return esc.replace(/快照 ([\w.\-]+\.html)/, (_, n) => `快照 <a target="_blank" href="/api/snapshots/${encodeURIComponent(n)}">${n}</a>`);
    },

    // ---- 影片 ----
    async loadVideos() {
      const p = new URLSearchParams({ q: this.vq.q, filter: this.vq.filter, page: this.vq.page, size: this.vq.size });
      if (this.vq.library_id) p.set("library_id", this.vq.library_id);
      this.videos = await this.get("/api/videos?" + p).catch(() => this.videos);
    },
    async refreshVideo(v) {
      this.notify(`正在刷新 ${v.slug} …`);
      v._busy = true;
      try {
        const nv = await this.post(`/api/videos/${v.slug}/refresh`);
        Object.assign(v, nv);
        const row = this.videos.items.find(x => x.slug === v.slug);
        if (row && row !== v) Object.assign(row, nv);
        if (this.detail && this.detail.slug === v.slug && this.detail !== v) Object.assign(this.detail, nv);
        this.notify(`${v.slug} 已刷新`);
      } finally { v._busy = false; }
    },
    openDetail(v) { this.detail = v; },
    async probeVideo(v) {
      this.notify(`正在到其他站点找 ${v.slug} …`);
      v._busy = true;
      try {
        const nv = await this.post(`/api/videos/${v.slug}/probe`);
        const before = (v.sources || []).length;
        Object.assign(v, nv);
        const row = this.videos.items.find(x => x.slug === v.slug);
        if (row && row !== v) Object.assign(row, nv);
        const n = (nv.sources || []).length - before;
        this.notify(n > 0 ? `找到 ${n} 个新的源` : "其他站点上没有找到");
      } finally { v._busy = false; }
    },
    srcState(src) {
      const now = Date.now() / 1000;
      if (src.status === "gone") return { text: "已下架", cls: "err" };
      if (src.status === "disabled") return { text: "已禁用", cls: "" };
      if (src.cooldown_until > now) return { text: "失败冷却 " + this.fmtDur(src.cooldown_until - now), cls: "warn" };
      if (!src.stream_url) return { text: "未缓存", cls: "" };
      if (!src.expires_stream) return { text: "长期有效", cls: "ok" };
      if (!src.stream_expires) return { text: "有效期未知", cls: "warn" };
      const left = src.stream_expires - now;
      if (left <= 0) return { text: "已过期", cls: "" };
      return { text: "剩 " + this.fmtDur(left), cls: left > 3600 ? "ok" : "warn" };
    },
    sourceRows(v) {
      // 多线路站点的源下面列出各条线路
      return (v.sources || []).flatMap(src => [{ key: "s" + src.id, src, line: null },
        ...(src.lines || []).map(ln => ({ key: "l" + ln.id, src, line: ln }))]);
    },
    lineSpec(site, name) {
      return (this.siteMeta(site).lines || []).find(l => l.name === name) || { name, supported: true, direct: true, note: "站点上新出现的线路，按页面内容识别播放站" };
    },
    lineModeText(spec, cfg) {
      if (!spec.supported) return "暂不支持";
      if (!spec.direct || cfg?.proxy) return "中转";
      return spec.ip_bound ? "302（直链绑出口 IP）" : "302";
    },
    moveLine(site, i, d) {
      const order = this.draft.sites[site].line_order;
      [order[i], order[i + d]] = [order[i + d], order[i]];
    },
    srcTitle(src) { return `${src.label} ${src.key}：${this.srcState(src).text}` + (src.last_error ? `\n${src.last_error}` : ""); },
    subtitleName(code) { return SUBTITLES[code ?? ""] || code; },
    subtitleTag(code) { return code === "zh" ? "·中字" : code === "en" ? "·英字" : ""; },
    async copy(text) {
      let ok = false;
      if (navigator.clipboard && window.isSecureContext) {
        try { await navigator.clipboard.writeText(text); ok = true; } catch {}
      }
      if (!ok) {
        // 非 https / 非 localhost 访问时 clipboard API 不可用，退回 execCommand
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;left:-9999px;top:0";
        document.body.appendChild(ta);
        ta.select();
        try { ok = document.execCommand("copy"); } catch {}
        ta.remove();
      }
      if (ok) this.notify("已复制：" + text);
      else prompt("复制失败，请手动复制", text);
    },
    async playVideo(v, source = null, line = null) {
      this.closePlayer();
      this.player = { slug: v.slug, title: (source ? `[${source.label}${line ? " " + line.line : ""}] ` : "") + v.title };
      const token = new URL(v.play_url, location.href).searchParams.get("t");
      const src = `/play/${v.slug}.m3u8?proxy=1` + (source ? `&src=${source.site}` : "") + (line ? `&line=${encodeURIComponent(line.line)}` : "")
        + (token ? `&t=${encodeURIComponent(token)}` : "");
      await this.$nextTick();
      const video = this.$refs.video;
      if (video.canPlayType("application/vnd.apple.mpegurl")) { video.src = src; return; }
      if (!window.Hls) {
        await new Promise((ok, fail) => {
          const sc = document.createElement("script");
          sc.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.min.js";
          sc.onload = ok; sc.onerror = () => fail(new Error("hls.js 加载失败"));
          document.head.appendChild(sc);
        }).catch(e => this.notify(e.message, true));
      }
      if (!window.Hls) return;
      this.hls = new Hls();
      this.hls.on(Hls.Events.ERROR, (_, d) => { if (d.fatal) this.notify("播放失败：" + d.details, true); });
      this.hls.loadSource(src);
      this.hls.attachMedia(video);
    },
    closePlayer() {
      if (this.hls) { this.hls.destroy(); this.hls = null; }
      this.player = null;
    },

    // ---- 输出库与订阅 ----
    async loadLibraries() { this.libraries = await this.get("/api/libraries").catch(() => this.libraries); },
    async loadSubs() { this.subscriptions = await this.get("/api/subscriptions").catch(() => this.subscriptions); },
    emptyRule() { return { categories: "", tags: "", models: "", quality: "", keywords: "", match: "any" }; },
    resetLibForm() {
      this.libForm = { id: null, name: "", dir: "", path_template: "", external_dir: "", sources: [], excludes: [], useRule: false, rule: this.emptyRule() };
    },
    editLibrary(l) {
      const rule = this.emptyRule();
      if (l.rule) for (const k of Object.keys(rule)) rule[k] = Array.isArray(l.rule[k]) ? l.rule[k].join(", ") : (l.rule[k] || rule[k]);
      this.libForm = { id: l.id, name: l.name, dir: l.dir, path_template: l.path_template, external_dir: l.external_dir,
                       sources: [...l.sources], excludes: [...l.excludes], useRule: !!l.rule, rule };
    },
    libNames(ids) { return ids.map(id => this.libraries.find(l => l.id === id)?.name || `#${id}`).join("、"); },
    async verifyLibrary(l) {
      const name = l ? `「${l.name}」` : "全部库";
      if (!confirm(`核对${name}：检查数据库里的每条输出在磁盘上还在不在（strm、nfo、封面），缺的补回。\n`
                   + "strm 和 nfo 在本地重写，封面先从别的库硬链接、没有再下载。\n"
                   + "外部整理库按内容找文件（外部工具改名、加后缀也认得出），找到只更新路径；两边都找不到才写回收件目录；外部整理目录是空的不补。")) return;
      const r = await this.post("/api/jobs", { kind: "verify", library_id: l ? l.id : null, repair: true, covers: true });
      this.notify(`已创建核对任务 #${r.id}`);
      if (this.tab === "jobs") this.loadJobs();
    },
    async loadFacets() { this.facets = await this.get("/api/facets").catch(() => this.facets); },
    async saveLibrary() {
      const f = this.libForm;
      const body = { name: f.name, dir: f.dir, path_template: f.path_template, external_dir: f.external_dir,
                     sources: f.sources.map(Number), excludes: f.excludes.map(Number), rule: f.useRule ? f.rule : null };
      try {
        const r = f.id ? await this.req("PUT", `/api/libraries/${f.id}`, body) : await this.req("POST", "/api/libraries", body);
        const jobs = [r.rewrite_job_id && `重写 #${r.rewrite_job_id}`, r.reclassify_job_id && `重新归库 #${r.reclassify_job_id}`,
                      r.locate_job_id && `同步位置 #${r.locate_job_id}`].filter(Boolean);
        this.notify((f.id ? "已保存" : "已新建输出库") + (jobs.length ? `，已排队：${jobs.join("、")}` : ""));
        this.resetLibForm();
        this.loadLibraries();
      } catch (e) { this.notify(e.message, true); }
    },
    async deleteLibrary(l) {
      if (!confirm(`删除输出库「${l.name}」？`)) return;
      const what = l.external_dir ? `${l.videos} 部影片的 strm（外部整理库只删 strm，nfo 和图片留给外部工具）` : `目录下本程序生成的 ${l.videos} 部影片的文件`;
      const files = confirm(`同时删除「${l.name}」${what}吗？\n确定 = 删除文件；取消 = 只删记录，保留文件`);
      try {
        const r = await this.req("DELETE", `/api/libraries/${l.id}?delete_files=${files}`);
        this.notify(`已排队删除任务 #${r.job_id}`);
        setTimeout(() => this.loadLibraries(), 1500);
      } catch (e) { this.notify(e.message, true); }
    },
    resetSubForm() {
      this.subForm = { id: null, name: "", site: "jable", source: "", sort: "post_date", library_id: 1, interval: 60, stop_after_known: 48,
                       max_pages: 20, detail: true, enabled: true, initial_full: true };
    },
    editSub(sub) {
      this.subForm = { id: sub.id, name: sub.name, site: sub.site, source: sub.source, sort: sub.sort, library_id: sub.library_id,
                       interval: sub.interval, stop_after_known: sub.stop_after_known, max_pages: sub.max_pages,
                       detail: !!sub.detail, enabled: !!sub.enabled, initial_full: false };
    },
    async saveSub() {
      const { id, ...body } = this.subForm;
      try {
        if (id) await this.req("PUT", `/api/subscriptions/${id}`, body);
        else await this.req("POST", "/api/subscriptions", body);
        this.notify(id ? "订阅已保存" : "订阅已新建");
        this.resetSubForm();
        this.loadSubs();
        this.refreshStatus();
      } catch (e) { this.notify(e.message, true); }
    },
    async deleteSub(sub) {
      if (!confirm(`删除订阅「${sub.name}」？（不影响已输出的文件）`)) return;
      try { await this.req("DELETE", `/api/subscriptions/${sub.id}`); this.loadSubs(); this.refreshStatus(); }
      catch (e) { this.notify(e.message, true); }
    },
    async runSub(sub, mode) {
      if (mode === "full" && !confirm(`对订阅「${sub.name}」跑一轮全量（翻完全部页）？全站约 3.9 万部：列表约 30 分钟，详情约 11 小时。中途可以暂停或重启，会自动续跑。`)) return;
      const r = await this.post(`/api/subscriptions/${sub.id}/run?mode=${mode}`);
      this.notify(`已创建任务 #${r.job_id}`);
      this.loadSubs();
      this.refreshStatus();
    },
    async markInitialized(sub) {
      if (!confirm(`订阅「${sub.name}」不跑首轮全量，直接改为定时增量？（库里已经用别的任务抓全时使用）`)) return;
      await this.post(`/api/subscriptions/${sub.id}/initialized`);
      this.loadSubs();
    },
    subState(sub) {
      if (sub.active_job_id) return { text: "执行中", cls: "warn" };
      if (!sub.enabled) return { text: "已停用", cls: "" };
      if (!sub.initialized) return { text: "未跑首轮全量", cls: "" };
      return { text: sub.interval ? "定时增量" : "仅手动", cls: "ok" };
    },

    // ---- strm 管理 ----
    strmKindName(k) { return STRM_KINDS[k] || k; },
    async loadScans() {
      const wasRunning = this.scans.find(j => j.id === this.scanId)?.status === "running";
      this.scans = await this.get("/api/strm/scans").catch(() => this.scans);
      const current = this.scans.find(j => j.id === this.scanId);
      if (!current && this.scans.length) this.selectScan(this.scans[0].id);
      else if (current && wasRunning && current.status !== "running") this.selectScan(current.id);
    },
    async startScan() {
      try {
        const r = await this.req("POST", "/api/strm/scan", { dir: this.scanDir });
        this.notify(`已开始扫描（任务 #${r.job_id}）`);
        this.scanId = r.job_id;
        this.scanSummary = null;
        setTimeout(() => this.loadScans(), 800);
      } catch (e) { this.notify(e.message, true); }
    },
    async selectScan(id) {
      this.scanId = id;
      this.sf = { kind: "", managed: "", prefix: "", q: "", page: 1, size: 50 };
      this.showMissing = false;
      this.scanSummary = await this.get(`/api/strm/scans/${id}/summary`).catch(() => null);
      this.loadStrmFiles();
    },
    async loadStrmFiles() {
      if (!this.scanId) return;
      const p = new URLSearchParams({ ...this.sf });
      this.strmFiles = await this.get(`/api/strm/scans/${this.scanId}/files?${p}`).catch(() => this.strmFiles);
    },
    async loadMissing() {
      if (this.showMissing) this.missing = await this.get(`/api/strm/scans/${this.scanId}/missing?size=200`).catch(() => this.missing);
    },
    async startAdopt() {
      const f = this.adoptForm;
      if (f.kinds.includes("named") && !confirm("「文件名识别」的文件内容会被改成本服务地址。确认这些文件都是 Jable 的吗？")) return;
      try {
        const r = await this.req("POST", "/api/strm/adopt", { scan_id: this.scanId, ...f, library_id: f.library_id || null });
        this.notify(`已创建纳管任务 #${r.job_id}，完成后重新扫描可看到结果`);
      } catch (e) { this.notify(e.message, true); }
    },
    async previewPrefix() {
      try {
        this.prefixForm.preview = await this.req("POST", "/api/strm/prefix/preview",
          { scan_id: this.scanId, old: this.prefixForm.old, new: this.prefixForm.new });
      } catch (e) { this.notify(e.message, true); }
    },
    async applyPrefix() {
      const f = this.prefixForm;
      if (!confirm(`把 ${f.preview.count} 个 strm 的前缀 ${f.old} 改成 ${f.new}？改动会记录下来，可以回滚。`)) return;
      try {
        const r = await this.req("POST", "/api/strm/prefix/apply", { scan_id: this.scanId, old: f.old, new: f.new });
        this.notify(`已创建改前缀任务 #${r.job_id}`);
        f.preview = null;
        setTimeout(() => { this.loadChangeSets(); this.selectScan(this.scanId); }, 1500);
      } catch (e) { this.notify(e.message, true); }
    },
    async loadChangeSets() { this.changeSets = await this.get("/api/strm/changes").catch(() => this.changeSets); },
    async revertChange(cs) {
      if (!confirm(`回滚改动 #${cs.change_set}（${cs.params.old} → ${cs.params.new}）？已被别处改过的文件会跳过。`)) return;
      try {
        const r = await this.req("POST", `/api/strm/changes/${cs.change_set}/revert`);
        this.notify(`已创建回滚任务 #${r.job_id}`);
        setTimeout(() => { this.loadChangeSets(); this.selectScan(this.scanId); }, 1500);
      } catch (e) { this.notify(e.message, true); }
    },

    // ---- 设置 ----
    async loadSettings() {
      this.settings = await this.get("/api/settings");
      this.draft = JSON.parse(JSON.stringify(this.settings.values));
    },
    fieldType(k) {
      const sc = this.settings.schema?.[k];
      if (k === "sites") return "sites";
      if (!sc) return "text";
      if (sc.enum) return "enum";
      if (sc.type === "boolean") return "bool";
      if (sc.type === "integer" || sc.type === "number") return "number";
      if (sc.type === "array") return "list";
      return "text";
    },
    enumName(k, v) { return k === "play_mode" ? PLAY_MODES[v] || v : v; },
    isChanged(k) { return JSON.stringify(this.draft[k]) !== JSON.stringify(this.settings.values?.[k]); },
    changedKeys() { return Object.keys(this.draft).filter(k => this.isChanged(k)); },
    async saveSettings() {
      const keys = this.changedKeys();
      const patch = Object.fromEntries(keys.map(k => [k, this.draft[k]]));
      try {
        const r = await this.req("PUT", "/api/settings", patch);
        this.settings.values = r.values;
        this.settings.effective = r.effective;
        this.draft = JSON.parse(JSON.stringify(r.values));
        if (keys.some(k => REWRITE_KEYS.includes(k))) this.needRewrite = true;
        this.notify("设置已保存并生效");
        this.refreshStatus();
      } catch (e) { this.notify(e.message, true); }
    },
    playModeName(m) { return PLAY_MODES[m] || m || "-"; },
    async testSolver(mode) {
      this.solverTest = { busy: true, mode, result: null };
      try {
        const result = await this.req("POST", "/api/solver/test", { url: this.draft.solver_url || "", mode, site: this.solverSite });
        this.solverTest = { busy: false, mode, result };
      } catch (e) {
        this.solverTest = { busy: false, mode, result: { ok: false, error: e.message } };
      }
    },
    solverResultText() {
      const { mode, result: r } = this.solverTest;
      if (!r) return "";
      if (r.error) return (mode === "solve" ? "解题失败：" : "连不上：") + r.error;
      const ms = r.ms >= 1000 ? (r.ms / 1000).toFixed(1) + " 秒" : r.ms + " ms";
      if (mode === "solve") {
        return r.ok ? `通过挑战：HTTP ${r.status}，拿到 cookie ${r.cookies} 个（${ms}）`
                    : `没通过挑战：HTTP ${r.status}（${ms}），可能需要换代理`;
      }
      if (!r.ok) return `能连上但服务出错：HTTP ${r.status}`;
      const name = [r.service, r.version].filter(Boolean).join(" ");
      return `已连上${name ? "：" + name : ""}（${ms}）` + (r.warning ? `。${r.warning}` : "");
    },

    // ---- 日志 ----
    connectLogs() {
      const after = this.logs.length ? this.logs[this.logs.length - 1].id : 0;
      const es = new EventSource(`/api/logs/stream?after=${after}`);
      es.onopen = () => (this.logConnected = true);
      es.onmessage = ev => {
        const item = JSON.parse(ev.data);
        if (this.logs.length && item.id <= this.logs[this.logs.length - 1].id) return;
        this.logs.push(item);
        if (this.logs.length > 3000) this.logs.splice(0, this.logs.length - 3000);
        this.$nextTick(() => this.scrollLogs());
      };
      es.onerror = () => {
        this.logConnected = false;
        es.close();
        setTimeout(() => this.connectLogs(), 3000);
      };
    },
    filteredLogs() {
      const min = LEVELS[this.logLevel] || 0;
      const kw = this.logFilter.trim().toLowerCase();
      return this.logs.filter(l => (LEVELS[l.level] || 0) >= min && (!kw || l.msg.toLowerCase().includes(kw) || l.name.includes(kw)));
    },
    scrollLogs(force = false) {
      for (const el of [this.$refs.logBox, this.$refs.miniLogs]) {
        if (el && (force || this.logFollow)) el.scrollTop = el.scrollHeight;
      }
    },

    // ---- 格式化 ----
    fmtNum(n) { return n == null ? "-" : Number(n).toLocaleString("zh-CN"); },
    pct(a, b) { return b ? Math.round((a || 0) * 100 / b) + "%" : "-"; },
    fmtTime(ts) {
      if (!ts) return "";
      const d = new Date(ts * 1000), p = x => String(x).padStart(2, "0");
      return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    },
    fmtClock(ts) { return this.fmtTime(ts).slice(6); },
    fmtDur(sec) {
      if (sec == null || isNaN(sec)) return "-";
      sec = Math.max(0, Math.round(sec));
      if (sec < 60) return sec + " 秒";
      if (sec < 3600) return Math.floor(sec / 60) + " 分 " + (sec % 60) + " 秒";
      if (sec < 86400) return Math.floor(sec / 3600) + " 小时 " + Math.floor(sec % 3600 / 60) + " 分";
      return Math.floor(sec / 86400) + " 天 " + Math.floor(sec % 86400 / 3600) + " 小时";
    },
    fmtClockDur(sec) {
      if (!sec) return "";
      const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60, p = x => String(x).padStart(2, "0");
      return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
    },
  };
}
