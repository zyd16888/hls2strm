// 后端 JSON API 的请求封装和数据类型（字段和 hls2strm/api.py 对应）

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

let onUnauthorized: () => void = () => {};

/** 接口返回 401（没登录、会话过期）时调用；登录接口自己的 401（密码错）不算。 */
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const r = await fetch(url, init);
  const text = await r.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!r.ok) {
    if (r.status === 401 && url !== "/api/login") onUnauthorized();
    const detail = (data as { detail?: unknown } | null)?.detail;
    const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map(d => d.msg).join("；") : "";
    throw new ApiError(msg || `HTTP ${r.status}`, r.status);
  }
  return data as T;
}

export const api = {
  get: <T>(url: string) => req<T>("GET", url),
  post: <T = { ok: boolean }>(url: string, body: unknown = {}) => req<T>("POST", url, body),
  put: <T = { ok: boolean }>(url: string, body: unknown) => req<T>("PUT", url, body),
  del: <T = { ok: boolean }>(url: string) => req<T>("DELETE", url),
};

/** 查询参数：数组按重复键展开（has_site=a&has_site=b），空值省略。 */
export function qs(params: Record<string, string | number | boolean | string[] | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === "") continue;
    if (Array.isArray(v)) v.forEach(x => p.append(k, x));
    else p.set(k, String(v));
  }
  const s = p.toString();
  return s ? "?" + s : "";
}

// ---- 状态 ----

export interface Domain {
  base: string;
  host: string;
  cooldown_until: number;
  cooling: number;
  block_streak: number;
  ok: number;
  blocked: number;
  errors: number;
  last_status: string;
  last_ok_at: number;
  solver_cookie: boolean;
}

export interface SiteStatus {
  name: string;
  label: string;
  enabled: boolean;
  direct: boolean;
  domains: Domain[];
  rate: { limit: number; current: number };
  concurrency: number;
  blocked_for: number;
}

export interface RunningTask {
  id: number;
  job_id: number;
  kind: string;
  site: string | null;
  target: string;
  attempt: number;
  started: number;
}

export interface Subscription {
  id: number;
  name: string;
  site: string;
  source: string;
  sort: string;
  library_id: number;
  library_name: string;
  detail: number;
  interval: number;
  stop_after_known: number;
  max_pages: number;
  enabled: number;
  initialized: number;
  last_run_at: number | null;
  last_job_id: number | null;
  active_job_id: number | null;
  created_at: number;
}

export interface Failure {
  id: number;
  job_id: number;
  kind: string;
  target: string;
  last_error: string;
  updated_at: number;
}

export type QueueCounts = Partial<Record<TaskStatus, number>>;

export interface Status {
  version: string;
  engine: { paused: boolean; blocked: Record<string, number>; blocked_for: number; workers: number; running: RunningTask[] };
  sites: SiteStatus[];
  solver: string;
  videos: { total: number; with_detail: number; gone: number; with_strm: number; with_cover: number };
  queue: Record<string, QueueCounts>;
  metrics: { uptime: number; requests_per_minute: number; counters: Record<string, number> };
  failures: Failure[];
  subscriptions: Subscription[];
  missing: number;
  output_dir: string;
  public_base_url: string;
  play_mode: string;
  now: number;
}

// ---- 站点元数据 ----

export interface LineSpecMeta {
  name: string;
  host: string;
  host_label: string;
  note: string;
  supported: boolean;
  direct: boolean;
  ip_bound: boolean;
}

export interface SiteMeta {
  label: string;
  presets: { name: string; source: string; sort: string }[];
  sorts: Record<string, string>;
  default_sort: string;
  hint: string;
  direct: boolean;
  ip_bound: boolean;
  lines: LineSpecMeta[];
}

export interface Meta {
  sites: Record<string, SiteMeta>;
}

// ---- 任务 ----

export type JobStatus = "running" | "paused" | "done" | "cancelled";
export type TaskStatus = "pending" | "running" | "done" | "failed" | "gone" | "cancelled";

export interface Job {
  id: number;
  kind: string;
  name: string;
  params: Record<string, unknown> & { library_id?: number | null; repair?: boolean; dir?: string };
  state: Record<string, number | boolean | string | undefined>;
  status: JobStatus;
  error: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  tasks: QueueCounts;
}

export interface Task {
  id: number;
  job_id: number;
  kind: string;
  target: string;
  site: string | null;
  status: TaskStatus;
  attempts: number;
  next_run_at: number;
  duration_ms: number | null;
  last_error: string;
  updated_at: number;
}

// ---- 影片 ----

export interface Named {
  id?: string;
  slug?: string;
  name: string;
}

interface StreamState {
  stream_url: string;
  stream_expires: number | null;
  expires_stream: boolean;
  cooldown_until: number;
  last_error: string;
  status?: string;
  /** 最高画质（分辨率的高），不知道为 null */
  height: number | null;
  /** 各档画质，从高到低，逗号分隔 */
  heights: string;
  /** 画质从哪来：master / embed / estimate / claimed，不知道为空 */
  quality_src: string;
  /** 播放站的连通性：0 正常（或不知道）/ 1 慢 / 2 不稳 / 3 不通 */
  health: number;
}

export interface HostHealth {
  key: string;
  label: string;
  tier: number;
  tier_name: string;
  /** 成功率的指数平均，0–1 */
  score: number;
  kbps: number;
  ttfb_ms: number;
  ok: number;
  fail: number;
  last_ok_at: number;
  last_fail_at: number;
  last_error: string;
  checked_at: number;
}

export interface HealthResponse {
  hosts: HostHealth[];
  checking: boolean;
  /** 下次定时检测的时间，关了为 0 */
  next_at: number;
}

export interface SourceLine extends StreamState {
  id: number;
  source_id: number;
  line: string;
  host: string;
  host_label: string;
  enabled: boolean;
  supported: boolean;
  direct: boolean;
  ip_bound: boolean;
}

export interface Source extends StreamState {
  id: number;
  video_id: number;
  site: string;
  key: string;
  label: string;
  title: string;
  subtitle: string;
  status: "active" | "gone" | "disabled";
  direct: boolean;
  line: string;
  lines?: SourceLine[];
  page_url: string;
  rank: number | null;
  fail_streak: number;
}

export interface Output {
  library_id: number;
  library_name: string;
  strm_path: string;
}

export interface Video {
  id: number;
  slug: string;
  code: string;
  title: string;
  duration: number | null;
  thumb_url: string;
  cover_url: string;
  views: number | null;
  favs: number | null;
  release_date: string;
  quality: string;
  models: Named[];
  categories: Named[];
  tags: Named[];
  maker: string;
  director: string;
  series: string;
  uncensored: number;
  status: "active" | "gone";
  detail_at: number | null;
  created_at: number;
  play_url: string;
  sources: Source[];
  outputs: Output[];
}

export interface ProbeResult {
  site: string;
  label: string;
  status: "found" | "none" | "failed" | "timeout";
  found?: number;
  error?: string;
}

export interface Page<T> {
  items: T[];
  total: number;
}

export interface FacetItem {
  item: string;
  name: string;
  n: number;
}

export type FacetField = "models" | "categories" | "tags" | "makers" | "quality";

// ---- 输出库 ----

export interface Rule {
  categories: string[];
  tags: string[];
  models: string[];
  quality: string[];
  keywords: string[];
  match: "any" | "all";
}

export interface Library {
  id: number;
  name: string;
  dir: string;
  path_template: string;
  rule: Rule | null;
  rule_text: string;
  external_dir: string;
  external_root: string;
  root: string;
  /** 多画质版本文件：空 = 不写，emby / suffix 是命名方式 */
  versions: "" | "emby" | "suffix";
  sources: number[];
  excludes: number[];
  videos: number;
  pending: number;
  subscriptions: number;
  missing: number;
}

// ---- strm 管理 ----

export interface ScanSummary {
  kinds: Record<string, { total: number; managed: number }>;
  prefixes: { prefix: string; n: number; managed: number; sample_url: string }[];
  adoptable: number;
  adoptable_unknown: number;
  job: Job;
  public_base_url: string;
}

export interface StrmFile {
  path: string;
  kind: string;
  slug: string | null;
  url: string;
  managed: number;
  video_id: number | null;
  note: string;
  expired: number;
}

export interface MissingOutput {
  slug: string;
  library_name: string;
  strm_path: string;
}

export interface ChangeSet {
  change_set: number;
  params: { old: string; new: string };
  files: number;
  reverted: number;
}

export interface PrefixPreview {
  count: number;
  managed: number;
  updates_setting: boolean;
  samples: { path: string; old: string; new: string }[];
}

// ---- 设置 ----

export interface SchemaProp {
  type?: string;
  enum?: string[];
  items?: { type?: string; enum?: string[] };
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  description?: string;
  default?: unknown;
}

export interface LineConfig {
  enabled: boolean;
  proxy: boolean;
}

export interface SiteConfig {
  enabled: boolean;
  domains: string[];
  rate_per_sec: number;
  concurrency: number;
  solver: boolean;
  lines: Record<string, LineConfig>;
  line_order: string[];
}

export type SettingsValues = Record<string, unknown> & { sites: Record<string, SiteConfig> };

export interface SettingsResponse {
  values: SettingsValues;
  schema: Record<string, SchemaProp>;
  effective: { output_dir: string; public_base_url: string };
}

// ---- 日志 ----

export interface LogItem {
  id: number;
  ts: number;
  level: string;
  name: string;
  msg: string;
}
