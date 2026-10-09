import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, Undo2 } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { useReorder } from "@/components/use-reorder";
import { Chip, Mono, Notice, Panel, Table, Td, Th } from "@/components/ui/data";
import { Check, Field, Input, Select, Switch, Textarea } from "@/components/ui/form";
import { rewriteAll } from "@/lib/actions";
import { api, type LineConfig, type LineSpecMeta, type SchemaProp, type SettingsResponse, type SettingsValues, type SiteConfig } from "@/lib/api";
import { PLAY_MODES, RESOLVE_MODES, VARIANT_MODES } from "@/lib/labels";
import { useMeta, useRun } from "@/lib/queries";
import { cn } from "@/lib/utils";

const GROUPS: { id: string; title: string; keys: string[] }[] = [
  { id: "sites", title: "站点", keys: ["sites", "site_priority"] },
  { id: "fetch", title: "抓取", keys: ["proxy", "impersonate", "request_timeout", "domain_cooldown", "solver_url", "solver_timeout"] },
  { id: "retry", title: "重试", keys: ["max_attempts", "retry_base_delay"] },
  { id: "tasks", title: "任务", keys: ["fetch_detail", "auto_probe_sites", "probe_recheck_days", "external_restore"] },
  { id: "output", title: "输出", keys: ["output_dir", "path_template", "write_nfo", "download_cover", "poster_crop"] },
  {
    id: "play",
    title: "播放",
    keys: [
      "public_base_url",
      "play_mode",
      "proxy_user_agents",
      "play_token",
      "hls_margin",
      "subtitle_priority",
      "subtitle_fallback",
      "quality_first",
      "quality_max",
      "quality_unknown",
      "prefer_direct",
      "variant_mode",
      "version_min_height",
      "quality_capture",
      "play_discover",
      "resolve_timeout",
      "resolve_attempt_timeout",
      "resolve_token",
      "resolve_mode",
      "resolve_proxy_url",
    ],
  },
  { id: "health", title: "连通性", keys: ["health_rank", "health_interval", "health_samples", "health_bytes", "health_slow_kbps"] },
];

const LABELS: Record<string, string> = {
  sites: "各站点",
  site_priority: "站点优先顺序",
  proxy: "抓取代理",
  impersonate: "浏览器指纹",
  request_timeout: "请求超时（秒）",
  domain_cooldown: "域名冷却（秒）",
  solver_url: "解题服务地址",
  solver_timeout: "解题超时（秒）",
  max_attempts: "最多尝试次数",
  retry_base_delay: "首次重试间隔（秒）",
  fetch_detail: "默认抓详情",
  auto_probe_sites: "新片自动补源",
  probe_recheck_days: "补源重查间隔（天）",
  health_rank: "按连通性挑源",
  health_interval: "定时检测间隔（分钟）",
  health_samples: "每个播放站抽几部",
  health_bytes: "每次下载多少（KB）",
  health_slow_kbps: "低于多少算慢（kbps）",
  external_restore: "外部整理库补回丢失的 strm",
  output_dir: "输出根目录",
  path_template: "默认路径模板",
  write_nfo: "写 nfo",
  download_cover: "下载封面",
  poster_crop: "裁剪 poster",
  public_base_url: "对外地址",
  play_mode: "播放模式",
  proxy_user_agents: "中转 UA 片段",
  play_token: "播放令牌",
  hls_margin: "有效期余量（分钟）",
  subtitle_priority: "字幕偏好",
  subtitle_fallback: "字幕回退",
  quality_first: "画质优先",
  quality_max: "画质上限",
  quality_unknown: "未知画质按",
  prefer_direct: "直连优先",
  variant_mode: "多码率的源给几档",
  version_min_height: "多画质版本最低档",
  quality_capture: "顺手探测画质",
  play_discover: "现场找源",
  resolve_timeout: "取地址总时限（秒）",
  resolve_attempt_timeout: "单源 / 线路尝试时限（秒）",
  resolve_token: "网关解析令牌",
  resolve_mode: "网关默认给的地址",
  resolve_proxy_url: "公网中转地址",
};
const REWRITE_KEYS = ["public_base_url", "play_mode", "play_token", "path_template", "output_dir", "write_nfo"];
const SUBTITLE_NAMES: Record<string, string> = { zh: "中文字幕", none: "无字幕", en: "英文字幕" };
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));

export default function Settings() {
  const qc = useQueryClient();
  const run = useRun();
  const { data } = useQuery({ queryKey: ["settings"], queryFn: ({ signal }) => api.get<SettingsResponse>("/api/settings", signal), staleTime: Infinity });
  const [draft, setDraft] = useState<SettingsValues | null>(null);
  const [needRewrite, setNeedRewrite] = useState(false);
  useEffect(() => {
    if (data) setDraft(clone(data.values));
  }, [data]);
  if (!data || !draft) return <div className="py-20 text-center text-sm text-muted">加载中…</div>;

  const changed = Object.keys(draft).filter(k => !same(draft[k], data.values[k]));
  const set = (k: string, v: unknown) => setDraft(d => (d ? { ...d, [k]: v } : d));

  const save = async () => {
    const patch = Object.fromEntries(changed.map(k => [k, draft[k]]));
    const r = await run(() => api.put<Pick<SettingsResponse, "values" | "effective">>("/api/settings", patch), {
      success: "设置已保存，马上生效",
      invalidate: [["status"], ["meta"]],
    });
    if (!r) return;
    qc.setQueryData<SettingsResponse>(["settings"], prev => (prev ? { ...prev, values: r.values, effective: r.effective } : prev));
    if (changed.some(k => REWRITE_KEYS.includes(k))) setNeedRewrite(true);
  };

  return (
    <div className="xl:grid xl:grid-cols-[160px_minmax(0,1fr)] xl:gap-6">
      <nav className="sticky top-16 hidden h-fit space-y-0.5 xl:block" aria-label="设置分组">
        {GROUPS.map(g => (
          <a key={g.id} href={`#/settings`} onClick={e => { e.preventDefault(); document.getElementById(`set-${g.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" }); }} className="block rounded-md px-2.5 py-1.5 text-sm text-muted hover:bg-panel-2 hover:text-ink">
            {g.title}
            {g.keys.some(k => changed.includes(k)) && <span className="ml-1.5 inline-block size-1.5 rounded-full bg-accent align-middle" />}
          </a>
        ))}
      </nav>
      <div className="space-y-4">
        <div className="sticky top-12 z-20 -mx-3 flex flex-wrap items-center gap-2 border-b border-line bg-bg/95 px-3 py-2.5 backdrop-blur sm:-mx-5 sm:px-5">
          <Button variant="primary" onClick={save} disabled={!changed.length}>
            <Save />
            保存{changed.length ? `（${changed.length} 项改动）` : ""}
          </Button>
          <Button onClick={() => setDraft(clone(data.values))} disabled={!changed.length}>
            <Undo2 />
            放弃改动
          </Button>
          <span className="ml-auto text-[13px] text-muted">
            生效的输出目录 <Mono>{data.effective.output_dir}</Mono>，对外地址 <Mono>{data.effective.public_base_url}</Mono>
          </span>
        </div>
        {needRewrite && (
          <Notice
            actions={
              <Button size="sm" variant="primary" onClick={() => rewriteAll(run).then(() => setNeedRewrite(false))}>
                重写全部输出
              </Button>
            }
          >
            改了影响 strm 内容或位置的设置，已有的文件要按新设置重写。
          </Notice>
        )}
        {GROUPS.map(g => (
          <Panel key={g.id} id={`set-${g.id}`} title={g.title} bodyClassName="divide-y divide-line py-0" className="scroll-mt-28">
            {g.keys
              .filter(k => data.schema[k])
              .map(k => (
                <Setting key={k} k={k} schema={data.schema[k]} changed={changed.includes(k)}>
                  <Control k={k} schema={data.schema[k]} value={draft[k]} draft={draft} set={set} />
                </Setting>
              ))}
          </Panel>
        ))}
      </div>
    </div>
  );
}

function Setting({ k, schema, changed, children }: { k: string; schema: SchemaProp; changed: boolean; children: ReactNode }) {
  const wide = k === "sites";
  return (
    <div className={cn("grid gap-x-6 gap-y-2 py-4", !wide && "md:grid-cols-[240px_minmax(0,1fr)]")}>
      <div className={cn("min-w-0", changed && "border-l-2 border-accent pl-2.5")}>
        <div className="text-sm font-medium">{LABELS[k] ?? k}</div>
        <code className="text-xs text-muted">{k}</code>
      </div>
      <div className="min-w-0 space-y-1.5">
        {children}
        {schema.description && <p className="max-w-[80ch] text-[13px] leading-relaxed text-muted">{schema.description}</p>}
      </div>
    </div>
  );
}

function Control({ k, schema, value, draft, set }: { k: string; schema: SchemaProp; value: unknown; draft: SettingsValues; set: (k: string, v: unknown) => void }) {
  const meta = useMeta();
  if (k === "sites") return <SitesEditor sites={draft.sites} onChange={v => set("sites", v)} />;
  if (k === "site_priority") return <OrderList items={value as string[]} label={meta.label} onChange={v => set(k, v)} />;
  if (k === "subtitle_priority") return <OrderList items={value as string[]} label={x => SUBTITLE_NAMES[x] ?? x} onChange={v => set(k, v)} />;
  if (k === "auto_probe_sites") {
    const v = value as string[];
    return (
      <div className="flex flex-wrap gap-x-5 gap-y-1.5">
        {Object.entries(meta.sites).map(([name, m]) => (
          <Check key={name} checked={v.includes(name)} onChange={on => set(k, on ? [...v, name] : v.filter(x => x !== name))}>
            {m.label}
          </Check>
        ))}
      </div>
    );
  }
  if (schema.enum)
    return (
      <Select value={String(value)} onChange={e => set(k, e.target.value)} className="w-full max-w-md">
        {schema.enum.map(o => (
          <option key={o} value={o}>
            {(k === "play_mode" ? PLAY_MODES : k === "resolve_mode" ? RESOLVE_MODES : k === "variant_mode" ? VARIANT_MODES : {})[o] ?? o}
          </option>
        ))}
      </Select>
    );
  if (schema.type === "boolean") return <Switch checked={!!value} onCheckedChange={v => set(k, v)} />;
  if (schema.type === "integer" || schema.type === "number")
    return (
      <Input
        type="number"
        className="w-36"
        step={schema.type === "integer" ? 1 : 0.1}
        min={schema.minimum ?? schema.exclusiveMinimum}
        max={schema.maximum}
        value={value as number}
        onChange={e => set(k, e.target.value === "" ? "" : Number(e.target.value))}
      />
    );
  if (schema.type === "array") return <Lines value={value as string[]} onChange={v => set(k, v)} />;
  return (
    <>
      <Input value={String(value ?? "")} onChange={e => set(k, e.target.value)} className="max-w-xl" />
      {k === "solver_url" && <SolverTest url={String(value ?? "")} />}
    </>
  );
}

/** 每行一个值的列表。 */
function Lines({ value, onChange, className }: { value: string[]; onChange: (v: string[]) => void; className?: string }) {
  const [text, setText] = useState(value.join("\n"));
  useEffect(() => {
    if (!same(text.split("\n").map(x => x.trim()).filter(Boolean), value)) setText(value.join("\n"));
  }, [value]);
  return (
    <Textarea
      className={cn("max-w-xl min-h-0", className)}
      rows={Math.max(2, text.split("\n").length)}
      value={text}
      onChange={e => {
        setText(e.target.value);
        onChange(e.target.value.split("\n").map(x => x.trim()).filter(Boolean));
      }}
    />
  );
}

/** 优先顺序在保存设置前只修改本地草稿。 */
function OrderList({ items, label, onChange }: { items: string[]; label: (x: string) => string; onChange: (v: string[]) => void }) {
  const reorder = useReorder(items, onChange);
  return (
    <div ref={reorder.root}>
    <p className="mb-1 text-xs text-muted">拖动手柄调整顺序，前面的优先；保存后生效。</p>
    <ol className="w-full max-w-sm divide-y divide-line rounded-md border border-line">
      {items.map((x, i) => (
        <li key={x} {...reorder.row(x)} className={cn("flex items-center gap-2 px-2.5 py-1.5 text-sm", reorder.row(x).className)}>
          {reorder.handle(x, label(x))}
          <span className="w-4 text-xs text-muted">{i + 1}</span>
          <span className="flex-1">{label(x)}</span>
        </li>
      ))}
    </ol>
    </div>
  );
}

function SitesEditor({ sites, onChange }: { sites: Record<string, SiteConfig>; onChange: (v: Record<string, SiteConfig>) => void }) {
  const meta = useMeta();
  const update = (name: string, patch: Partial<SiteConfig>) => onChange({ ...sites, [name]: { ...sites[name], ...patch } });
  return (
    <div className="grid gap-3 2xl:grid-cols-2">
      {Object.entries(sites).map(([name, cfg]) => {
        const m = meta.site(name);
        return (
          <div key={name} className={cn("rounded-md border border-line p-3", !cfg.enabled && "bg-panel-2/60")}>
            <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
              <b className="text-[15px]">{m.label}</b>
              <Chip>{m.direct ? (m.ip_bound ? "302（直链绑出口 IP，网关不用它）" : "可以 302") : "必须中转"}</Chip>
              <label className="ml-auto flex cursor-pointer items-center gap-2 text-[13px]">
                <Switch checked={cfg.enabled} onCheckedChange={v => update(name, { enabled: v })} />
                启用
              </label>
              <label className="flex cursor-pointer items-center gap-2 text-[13px]">
                <Switch checked={cfg.solver} onCheckedChange={v => update(name, { solver: v })} />
                被拦时用解题服务
              </label>
            </div>
            <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_110px_90px]">
              <Field label="域名（每行一个，前面的先用）">
                <Lines value={cfg.domains} onChange={v => update(name, { domains: v })} />
              </Field>
              <Field label="限速（次/秒）">
                <Input type="number" step={0.1} min={0.05} max={20} value={cfg.rate_per_sec} onChange={e => update(name, { rate_per_sec: Number(e.target.value) })} />
              </Field>
              <Field label="并发">
                <Input type="number" min={1} max={16} value={cfg.concurrency} onChange={e => update(name, { concurrency: Number(e.target.value) })} />
              </Field>
            </div>
            {m.lines.length > 0 && <LinesTable specs={m.lines} cfg={cfg} onChange={patch => update(name, patch)} />}
          </div>
        );
      })}
    </div>
  );
}

function LinesTable({ specs, cfg, onChange }: { specs: LineSpecMeta[]; cfg: SiteConfig; onChange: (patch: Partial<SiteConfig>) => void }) {
  const order = cfg.line_order.length ? cfg.line_order : specs.map(s => s.name);
  const spec = (name: string): LineSpecMeta =>
    specs.find(s => s.name === name) ?? { name, host: "", host_label: "", note: "站点上新出现的线路，按页面内容识别播放站", supported: true, direct: true, ip_bound: false };
  const lineCfg = (name: string): LineConfig => cfg.lines[name] ?? { enabled: true, proxy: false };
  const setLine = (name: string, patch: Partial<LineConfig>) => onChange({ lines: { ...cfg.lines, [name]: { ...lineCfg(name), ...patch } }, line_order: order });
  const reorder = useReorder(order, line_order => onChange({ line_order }));
  const mode = (s: LineSpecMeta, c: LineConfig) => (!s.supported ? "暂不支持" : !s.direct || c.proxy ? "中转" : s.ip_bound ? "302（绑出口 IP）" : "302");
  return (
    <div className="mt-3" ref={reorder.root}>
      <div className="mb-1 text-[13px] text-muted">线路（拖动手柄调整尝试顺序，保存后生效）</div>
      <Table className="text-[13px]">
        <thead>
          <tr>
            <Th>顺序</Th>
            <Th>线路</Th>
            <Th>播放站</Th>
            <Th>方式</Th>
            <Th>启用</Th>
            <Th>强制中转</Th>
          </tr>
        </thead>
        <tbody>
          {order.map((name, i) => {
            const s = spec(name);
            const c = lineCfg(name);
            return (
              <tr key={name} {...reorder.row(name)} className={cn(reorder.row(name).className, (!c.enabled || !s.supported) && "opacity-55")} title={s.note}>
                <Td className="whitespace-nowrap">
                  {reorder.handle(name, `线路 ${name}`)}
                  <span className="ml-1 text-xs text-muted">{i + 1}</span>
                </Td>
                <Td className="font-mono">{name}</Td>
                <Td>
                  {s.host_label || "-"}
                  {s.note && <div className="max-w-[260px] truncate text-xs text-muted">{s.note}</div>}
                </Td>
                <Td className="whitespace-nowrap">
                  <Chip>{mode(s, c)}</Chip>
                </Td>
                <Td>
                  <Switch checked={c.enabled} disabled={!s.supported} onCheckedChange={v => setLine(name, { enabled: v })} aria-label={`启用线路 ${name}`} />
                </Td>
                <Td>
                  <Switch checked={c.proxy} disabled={!s.direct} onCheckedChange={v => setLine(name, { proxy: v })} aria-label={`线路 ${name} 强制中转`} />
                </Td>
              </tr>
            );
          })}
        </tbody>
      </Table>
    </div>
  );
}

interface SolverResult {
  ok: boolean;
  error?: string;
  status?: number;
  ms?: number;
  cookies?: number;
  service?: string;
  version?: string;
  warning?: string;
}

function SolverTest({ url }: { url: string }) {
  const meta = useMeta();
  const [site, setSite] = useState("jable");
  const [state, setState] = useState<{ busy: boolean; mode: "ping" | "solve"; result: SolverResult | null }>({ busy: false, mode: "ping", result: null });
  const test = async (mode: "ping" | "solve") => {
    setState({ busy: true, mode, result: null });
    try {
      const result = await api.post<SolverResult>("/api/solver/test", { url, mode, site });
      setState({ busy: false, mode, result });
    } catch (e) {
      setState({ busy: false, mode, result: { ok: false, error: (e as Error).message } });
    }
  };
  const r = state.result;
  const ms = r?.ms != null ? (r.ms >= 1000 ? `${(r.ms / 1000).toFixed(1)} 秒` : `${r.ms} ms`) : "";
  let text = "";
  if (r) {
    if (r.error) text = (state.mode === "solve" ? "解题失败：" : "连不上：") + r.error;
    else if (state.mode === "solve") text = r.ok ? `通过挑战：HTTP ${r.status}，拿到 cookie ${r.cookies} 个（${ms}）` : `没通过挑战：HTTP ${r.status}（${ms}），可能要换代理`;
    else if (!r.ok) text = `能连上，但服务出错：HTTP ${r.status}`;
    else text = `已连上${[r.service, r.version].filter(Boolean).length ? "：" + [r.service, r.version].filter(Boolean).join(" ") : ""}（${ms}）${r.warning ? "。" + r.warning : ""}`;
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" onClick={() => test("ping")} disabled={state.busy || !url}>
        测试连通
      </Button>
      <Select value={site} onChange={e => setSite(e.target.value)} className="w-28" aria-label="试解哪个站点">
        {Object.entries(meta.sites).map(([k, m]) => (
          <option key={k} value={k}>
            {m.label}
          </option>
        ))}
      </Select>
      <Button size="sm" onClick={() => test("solve")} disabled={state.busy || !url} title="让解题服务实际打开一次所选站点的首选域名（走当前代理），最长等到解题超时">
        试解一次
      </Button>
      {state.busy && <span className="text-[13px] text-muted">{state.mode === "solve" ? "解题中，最长等到解题超时…" : "检测中…"}</span>}
      {r && !state.busy && <span className={cn("text-[13px]", r.ok ? "text-ok" : "text-err")}>{text}</span>}
    </div>
  );
}
