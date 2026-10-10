import { RotateCcw, Wifi } from "lucide-react";
import { useState } from "react";
import { api, type SiteStatus } from "@/lib/api";
import { fmtDur, fmtNum } from "@/lib/format";
import { DOMAIN_MODES } from "@/lib/labels";
import { useRun, useStatus } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";
import { Chip, Dot, type Tone } from "./ui/data";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/overlay";

export function siteSignal(st: SiteStatus, blockedFor = 0): { tone: Tone; text: string } {
  if (!st.enabled) return { tone: "neutral", text: "未启用" };
  if (blockedFor > 0 || st.blocked_for > 0) return { tone: "err", text: `被拦截，${fmtDur(blockedFor || st.blocked_for)}后重试` };
  const cooling = st.domains.filter(d => d.cooling > 0 || (st.domain_mode !== "priority" && d.error_cooling > 0)).length;
  if (cooling) return { tone: "warn", text: `${cooling} 个域名冷却中` };
  if (st.domains.some(d => d.ok > 0)) return { tone: "ok", text: "可用" };
  return { tone: "neutral", text: "还没请求过" };
}

interface TestResult {
  site: string;
  host: string;
  ok: boolean;
  status?: number;
  blocked?: boolean;
  ms?: number;
  error?: string;
}

/** 顶栏：每个站点一盏灯，点开看域名状态、测试连通性、重置冷却。 */
export function SiteSignals() {
  const { data } = useStatus();
  if (!data) return null;
  return (
    <div className="flex min-w-0 items-center gap-1 overflow-x-auto scroll-thin">
      {data.sites.map(st => (
        <SiteLamp key={st.name} st={st} blockedFor={data.engine.blocked[st.name] ?? 0} />
      ))}
    </div>
  );
}

function SiteLamp({ st, blockedFor }: { st: SiteStatus; blockedFor: number }) {
  const sig = siteSignal(st, blockedFor);
  const run = useRun();
  const [testing, setTesting] = useState(false);
  const [results, setResults] = useState<TestResult[] | null>(null);
  const test = async () => {
    setTesting(true);
    const r = await run(() => api.post<TestResult[]>(`/api/fetcher/test?site=${st.name}`));
    setTesting(false);
    if (r) setResults(r);
  };
  return (
    <Popover>
      <PopoverTrigger
        className={cn(
          "flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2 text-[13px] hover:bg-panel-2 data-[state=open]:bg-panel-2",
          !st.enabled && "text-muted",
        )}
        title={`${st.label}：${sig.text}`}
      >
        <Dot tone={sig.tone} pulse={sig.tone === "err"} />
        <span className="hidden md:inline">{st.label}</span>
      </PopoverTrigger>
      <PopoverContent className="w-80 p-0">
        <div className="flex items-center gap-2 border-b border-line px-3 py-2.5">
          <Dot tone={sig.tone} />
          <b className="text-sm">{st.label}</b>
          <Chip tone={sig.tone}>{sig.text}</Chip>
        </div>
        <div className="space-y-2 px-3 py-2.5 text-[13px]">
          <div className="flex gap-4 text-muted">
            <span>
              限速 <b className="text-ink">{st.rate.current}</b> / {st.rate.limit} 次/秒
            </span>
            <span>
              并发 <b className="text-ink">{st.concurrency}</b>
            </span>
          </div>
          <p className="text-xs text-muted">域名策略：{DOMAIN_MODES[st.domain_mode] ?? DOMAIN_MODES.priority}</p>
          <ul className="space-y-1.5">
            {st.domains.map(d => (
              <li key={d.base} className="flex items-start gap-2">
                <Dot tone={d.cooling ? "warn" : d.ok ? "ok" : "neutral"} className="mt-1.5" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className="font-mono text-[12.5px]">{d.host}</span>
                    <span className="ml-auto shrink-0 text-xs text-muted">
                      成功 {fmtNum(d.ok)} · 被拦 {fmtNum(d.blocked)}
                    </span>
                  </div>
                  <div className="truncate text-xs text-muted" title={d.last_status}>
                    {d.cooling ? `拦截冷却 ${fmtDur(d.cooling)}` : st.domain_mode !== "priority" && d.error_cooling ? `网络错误冷却 ${fmtDur(d.error_cooling)}` : d.last_status || "未使用"}
                  </div>
                  <div className="text-xs text-muted">处理中 {d.in_flight ?? 0} · 响应 {d.response_ms == null ? "未测" : `${d.response_ms.toFixed(0)} ms`}</div>
                </div>
              </li>
            ))}
          </ul>
          {results && (
            <ul className="space-y-1 rounded-md bg-panel-2 p-2 text-xs">
              {results.map(r => (
                <li key={r.host} className="flex gap-2">
                  <Chip tone={r.ok ? "ok" : "err"}>{r.ok ? "通" : "不通"}</Chip>
                  <span className="font-mono">{r.host}</span>
                  <span className="truncate text-muted">
                    {r.error || `HTTP ${r.status}${r.blocked ? "，被 CF 拦截" : ""}，${r.ms}ms`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="flex gap-1.5 border-t border-line px-3 py-2">
          <Button size="sm" onClick={test} disabled={testing || !st.enabled}>
            <Wifi />
            {testing ? "测试中…" : "测试连通性"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => run(() => api.post(`/api/fetcher/reset?site=${st.name}`), { success: `${st.label} 的冷却已重置` })}
          >
            <RotateCcw />
            重置冷却
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
