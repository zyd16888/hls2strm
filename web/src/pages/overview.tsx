import { Activity, RotateCcw, Wifi } from "lucide-react";
import { useState } from "react";
import { ErrorText } from "@/components/common";
import { LogLines } from "@/components/log-lines";
import { siteSignal } from "@/components/site-signals";
import { Button } from "@/components/ui/button";
import { Chip, Dot, EmptyRow, KV, Mono, Notice, Panel, Table, Td, Th, type Tone } from "@/components/ui/data";
import { rewriteAll, runSubscription, verifyLibrary } from "@/lib/actions";
import { api, type Status, type Subscription } from "@/lib/api";
import { fmtDur, fmtNum, fmtTime, pct } from "@/lib/format";
import { kindName, PLAY_MODES } from "@/lib/labels";
import { useLogs } from "@/lib/logs";
import { useHealth, useMeta, useRun, useStatus } from "@/lib/queries";
import { navigate } from "@/lib/route";
import { cn } from "@/lib/utils";

export function subState(sub: Subscription): { text: string; tone: "warn" | "neutral" | "ok" } {
  if (sub.listing_job_id) return { text: "扫描中", tone: "warn" };
  if (sub.active_job_id) return { text: "补充元数据", tone: "neutral" };
  if (!sub.enabled) return { text: "已停用", tone: "neutral" };
  if (!sub.initialized) return { text: "未跑首轮全量", tone: "neutral" };
  return { text: sub.interval ? "定时增量" : "仅手动", tone: "ok" };
}

export default function Overview() {
  const { data: s } = useStatus();
  const run = useRun();
  const meta = useMeta();
  if (!s) return <div className="py-20 text-center text-sm text-muted">正在连接服务…</div>;

  const queueSum = (k: "pending" | "running" | "failed") => Object.values(s.queue).reduce((a, q) => a + (q[k] ?? 0), 0);
  const c = (k: string) => s.metrics.counters[k] ?? 0;
  const blocked = Object.entries(s.engine.blocked);

  return (
    <>
      {blocked.length > 0 && (
        <Notice
          tone="warn"
          actions={
            <Button size="sm" onClick={() => run(() => api.post("/api/fetcher/reset"), { success: "已重置冷却，马上重试" })}>
              <RotateCcw />
              立即重试
            </Button>
          }
        >
          <b>{blocked.map(([k, v]) => `${meta.label(k)} 被拦截（${fmtDur(v)}后重试）`).join("，")}</b>
          。冷却结束后自动重试，其他站点照常抓取。
        </Notice>
      )}
      {s.missing > 0 && (
        <Notice
          tone="warn"
          actions={
            <>
              <Button size="sm" onClick={() => verifyLibrary(run, null)}>
                核对并补回
              </Button>
              <Button size="sm" variant="ghost" onClick={() => navigate("libraries")}>
                看是哪些库
              </Button>
            </>
          }
        >
          数据库里有记录、但磁盘上找不到的 strm 有 <b>{fmtNum(s.missing)}</b> 个。可能是换了输出目录的挂载，或者文件被删了。
        </Notice>
      )}

      <Metrics s={s} queueSum={queueSum} c={c} />
      <RequestTimings s={s} />

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel
          title="正在执行"
          actions={<span className="text-[13px] text-muted">{s.engine.workers} 个 worker，已运行 {fmtDur(s.metrics.uptime)}</span>}
        >
          {s.engine.running.length === 0 ? (
            <p className="py-3 text-sm text-muted">{s.engine.paused ? "引擎已暂停，恢复后继续执行排队的子任务。" : "当前没有执行中的子任务。"}</p>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>任务</Th>
                  <Th>类型</Th>
                  <Th>目标</Th>
                  <Th className="text-right">第几次</Th>
                  <Th className="text-right">已用时</Th>
                </tr>
              </thead>
              <tbody>
                {s.engine.running.map(r => (
                  <tr key={r.id}>
                    <Td className="text-muted">#{r.job_id}</Td>
                    <Td>{kindName(r.kind)}</Td>
                    <Td>
                      {r.site && <span className="mr-1.5 text-muted">{meta.label(r.site)}</span>}
                      <Mono>{r.target}</Mono>
                    </Td>
                    <Td className="text-right">{r.attempt}</Td>
                    <Td className="text-right">{fmtDur(s.now - r.started)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
          <KV
            className="mt-4 border-t border-line pt-4"
            items={[
              ["输出目录", <Mono key="o">{s.output_dir}</Mono>],
              ["strm 地址", <Mono key="p">{s.public_base_url}/play/…</Mono>],
              ["播放模式", PLAY_MODES[s.play_mode] ?? s.play_mode],
              ["解题服务", s.solver ? <Mono key="s">{s.solver}</Mono> : <span className="text-muted">没配置（被 CF 拦的站过不去）</span>],
            ]}
          />
        </Panel>

        <Channels s={s} />
      </div>

      <PlaybackHealth />

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel
          title="订阅"
          actions={
            <>
              <Button size="sm" variant="ghost" onClick={() => navigate("libraries")}>
                管理订阅
              </Button>
            </>
          }
        >
          <Table>
            <thead>
              <tr>
                <Th>订阅</Th>
                <Th>输出库</Th>
                <Th>状态</Th>
                <Th>上次运行</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {s.subscriptions.map(sub => {
                const st = subState(sub);
                return (
                  <tr key={sub.id}>
                    <Td>
                      <div>{sub.name}</div>
                      <div className="text-xs text-muted">
                        {meta.label(sub.site)} <Mono className="text-xs">{sub.source}</Mono>
                      </div>
                    </Td>
                    <Td className="whitespace-nowrap text-[13px]">{sub.library_name}</Td>
                    <Td>
                      <Chip tone={st.tone}>{st.text}</Chip>
                    </Td>
                    <Td className="whitespace-nowrap text-[13px] text-muted">{sub.last_run_at ? fmtTime(sub.last_run_at) : "-"}</Td>
                    <Td className="text-right">
                      {!sub.listing_job_id &&
                        (sub.initialized ? (
                          <Button size="sm" onClick={() => runSubscription(run, sub, "incremental")}>
                            立即增量
                          </Button>
                        ) : (
                          <Button size="sm" variant="primary" onClick={() => runSubscription(run, sub, "full")}>
                            开始首轮全量
                          </Button>
                        ))}
                    </Td>
                  </tr>
                );
              })}
              {s.subscriptions.length === 0 && <EmptyRow cols={5}>还没有订阅。到「输出库与订阅」新建一个，按周期自动跟进站点更新。</EmptyRow>}
            </tbody>
          </Table>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button
              onClick={() =>
                run(() => api.post<{ id: number }>("/api/jobs", { kind: "backfill" }), {
                  success: r => `已创建任务 #${r.id}`,
                  invalidate: [["status"], ["jobs"]],
                })
              }
            >
              补全缺失详情
            </Button>
            <Button onClick={() => rewriteAll(run)}>重写全部输出</Button>
          </div>
          <h3 className="mb-1 mt-5 text-sm font-semibold">活动任务的队列</h3>
          <Table>
            <thead>
              <tr>
                <Th>类型</Th>
                <Th className="text-right">待处理</Th>
                <Th className="text-right">运行中</Th>
                <Th className="text-right">完成</Th>
                <Th className="text-right">失败</Th>
                <Th className="text-right">下架</Th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(s.queue).map(([k, q]) => (
                <tr key={k}>
                  <Td>{kindName(k)}</Td>
                  <Td className="text-right">{fmtNum(q.pending ?? 0)}</Td>
                  <Td className="text-right">{fmtNum(q.running ?? 0)}</Td>
                  <Td className="text-right">{fmtNum(q.done ?? 0)}</Td>
                  <Td className={cn("text-right", q.failed && "text-err")}>{fmtNum(q.failed ?? 0)}</Td>
                  <Td className="text-right">{fmtNum(q.gone ?? 0)}</Td>
                </tr>
              ))}
              {Object.keys(s.queue).length === 0 && <EmptyRow cols={6}>没有活动任务</EmptyRow>}
            </tbody>
          </Table>
        </Panel>

        <Panel title="最近失败" actions={<Button size="sm" variant="ghost" onClick={() => navigate("jobs")}>看全部任务</Button>}>
          {s.failures.length === 0 ? (
            <p className="py-3 text-sm text-muted">最近没有失败的子任务。</p>
          ) : (
            <ul className="divide-y divide-line">
              {s.failures.map(f => (
                <li key={f.id} className="py-2 text-[13px] first:pt-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <Chip>{kindName(f.kind)}</Chip>
                    <Mono>{f.target}</Mono>
                    <span className="ml-auto text-xs text-muted">
                      任务 #{f.job_id} · {fmtTime(f.updated_at)}
                    </span>
                  </div>
                  <div className="mt-1">
                    <ErrorText msg={f.last_error} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <RecentLogs />
    </>
  );
}

function RequestTimings({ s }: { s: Status }) {
  const labels: Record<string, string> = {
    "play.resolve": "播放选源与刷新", "play.refresh_wait": "同源刷新排队", "play.playlist": "播放清单", "pool.play.wait": "播放连接排队",
    "pool.hls.wait": "HLS 连接排队", "pool.file.wait": "文件连接排队", "pool.preflight.wait": "预检连接排队", "pool.speed.wait": "测速连接排队", "upstream.play": "源站响应", "relay.ttfb": "中转首字节",
    "db.read": "数据读取", "db.bulk": "列表与统计查询", "task.output": "文件输出",
  };
  const rows = Object.entries(s.metrics.timings ?? {}).filter(([key]) => labels[key]);
  const gauges = s.metrics.gauges ?? {};
  return <Panel title="请求耗时">
    <p className="mb-2 text-xs text-muted">最近 15 分钟的服务端样本；302 后播放器的首帧时间由客户端网络决定。</p>
    <p className="mb-3 text-xs leading-relaxed text-muted">HLS 活动 {gauges.http_active_hls ?? 0} / 等待 {gauges.http_waiting_hls ?? 0} · 文件活动 {gauges.http_active_file ?? 0} / 等待 {gauges.http_waiting_file ?? 0} · 预检活动 {gauges.http_active_preflight ?? 0} / 等待 {gauges.http_waiting_preflight ?? 0} · 缓冲 {((gauges.relay_buffer_bytes ?? 0)/1024).toFixed(0)} KiB · 上游断流 {s.metrics.counters.relay_aborted ?? 0} · 日志缺口 {s.metrics.log_dropped ?? 0}</p>
    <Table><thead><tr><Th>阶段</Th><Th>样本</Th><Th>P50</Th><Th>P95</Th><Th>最大</Th></tr></thead><tbody>
      {rows.length ? rows.map(([key, v]) => <tr key={key}><Td>{labels[key]}</Td><Td>{v.count}</Td><Td>{v.p50_ms}ms</Td><Td>{v.p95_ms}ms</Td><Td>{v.max_ms}ms</Td></tr>) : <tr><Td colSpan={5}>还没有请求样本</Td></tr>}
    </tbody></Table>
  </Panel>;
}

function Metrics({ s, queueSum, c }: { s: Status; queueSum: (k: "pending" | "running" | "failed") => number; c: (k: string) => number }) {
  const rate = s.sites
    .filter(x => x.enabled)
    .map(x => `${x.label} ${x.rate.current}/${x.rate.limit}`)
    .join("，");
  const items: { label: string; value: string; sub: string; tone?: string; title?: string }[] = [
    { label: "影片", value: fmtNum(s.videos.total), sub: `下架 ${fmtNum(s.videos.gone)}` },
    { label: "已补详情", value: fmtNum(s.videos.with_detail), sub: pct(s.videos.with_detail, s.videos.total) },
    { label: "已写 strm", value: fmtNum(s.videos.with_strm), sub: `有封面 ${fmtNum(s.videos.with_cover)}` },
    { label: "待处理子任务", value: fmtNum(queueSum("pending")), sub: `运行中 ${queueSum("running")}` },
    { label: "失败子任务", value: fmtNum(queueSum("failed")), sub: "活动任务里", tone: queueSum("failed") ? "text-err" : "" },
    { label: "源站响应 / 分钟", value: fmtNum(s.metrics.requests_per_minute), sub: "当前限速 次/秒", title: rate },
    { label: "被拦截", value: fmtNum(c("fetch_blocked")), sub: `网络错误 ${fmtNum(c("fetch_error"))}` },
    {
      label: "播放请求",
      value: fmtNum(c("play_requests")),
      sub: `302 跳转 ${fmtNum(c("play_redirect"))}，中转 ${fmtNum(c("play_proxy"))}`,
    },
  ];
  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-4 2xl:grid-cols-8">
      {items.map(m => (
        <div key={m.label} title={m.title} className="bg-panel px-4 py-3">
          <div className="text-[13px] text-muted">{m.label}</div>
          <div className={cn("mt-0.5 text-[26px] font-semibold leading-tight tracking-tight", m.tone)}>{m.value}</div>
          <div className="truncate text-xs text-muted">{m.sub}</div>
        </div>
      ))}
    </div>
  );
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

const HEALTH_TONES: Tone[] = ["ok", "warn", "warn", "err"];

function PlaybackHealth() {
  const run = useRun();
  const { data } = useHealth();
  const hosts = data?.hosts ?? [];
  return (
    <Panel
      title="播放连通性"
      actions={
        <>
          <span className="text-[13px] text-muted">
            {data?.checking ? "检测中…" : data?.next_at ? `下次定时检测 ${fmtTime(data.next_at)}` : "定时检测已关"}
          </span>
          <Button
            size="sm"
            disabled={data?.checking}
            onClick={() => run(() => api.post("/api/health/check"), { success: "开始检测，结果稍后刷新", invalidate: [["health"]] })}
          >
            <Activity />
            立即检测
          </Button>
        </>
      }
    >
      <Table>
        <thead>
          <tr>
            <Th>播放站</Th>
            <Th>状态</Th>
            <Th className="text-right">成功率</Th>
            <Th className="text-right">速度</Th>
            <Th className="text-right">首字节</Th>
            <Th className="text-right">成功 / 失败</Th>
            <Th>上次检测</Th>
          </tr>
        </thead>
        <tbody>
          {hosts.map(h => {
            const known = h.ok + h.fail > 0;
            return (
              <tr key={h.key}>
                <Td className="font-medium">{h.label}</Td>
                <Td>
                  <Chip tone={known ? HEALTH_TONES[h.tier] : "neutral"}>{known ? h.tier_name : "没有数据"}</Chip>
                  {h.last_error && (
                    <div className="max-w-[280px] truncate text-xs text-muted" title={h.last_error}>
                      {h.last_error}
                    </div>
                  )}
                </Td>
                <Td className="text-right">{known ? pct(h.score, 1) : "-"}</Td>
                <Td className="text-right">{h.kbps ? `${(h.kbps / 1000).toFixed(1)} Mbps` : "-"}</Td>
                <Td className="text-right">{h.ttfb_ms ? `${Math.round(h.ttfb_ms)} ms` : "-"}</Td>
                <Td className="text-right">
                  {fmtNum(h.ok)} / {fmtNum(h.fail)}
                </Td>
                <Td className="whitespace-nowrap text-[13px] text-muted">{h.checked_at ? fmtTime(h.checked_at) : "-"}</Td>
              </tr>
            );
          })}
          {hosts.length === 0 && <EmptyRow cols={7}>还没有数据。播放过影片、或者检测一次之后，这里会列出各播放站能不能播、快不快。</EmptyRow>}
        </tbody>
      </Table>
      <p className="mt-3 text-[13px] text-muted">
        测的是本服务到各播放站 CDN 的网络：中转时完全准，302 给外网客户端时只能参考。开着「按连通性挑源」时，不通、不稳、慢的播放站排到后面。
      </p>
    </Panel>
  );
}

function Channels({ s }: { s: Status }) {
  const run = useRun();
  const [testing, setTesting] = useState(false);
  const [results, setResults] = useState<TestResult[] | null>(null);
  const meta = useMeta();
  return (
    <Panel
      title="抓取通道"
      actions={
        <>
          <Button
            size="sm"
            disabled={testing}
            onClick={async () => {
              setTesting(true);
              const r = await run(() => api.post<TestResult[]>("/api/fetcher/test"));
              setTesting(false);
              if (r) setResults(r);
            }}
          >
            <Wifi />
            {testing ? "测试中…" : "测试全部域名"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => run(() => api.post("/api/fetcher/reset"), { success: "已重置全部冷却" })}>
            <RotateCcw />
            重置冷却
          </Button>
        </>
      }
    >
      <Table>
        <thead>
          <tr>
            <Th>站点 / 域名</Th>
            <Th>状态</Th>
            <Th className="text-right">成功</Th>
            <Th className="text-right">被拦</Th>
            <Th className="text-right">错误</Th>
          </tr>
        </thead>
        <tbody>
          {s.sites.map(st => {
            const sig = siteSignal(st, s.engine.blocked[st.name] ?? 0);
            return st.domains.map((d, i) => (
              <tr key={st.name + d.base} className={st.enabled ? "" : "opacity-55"}>
                <Td>
                  {i === 0 && (
                    <div className="flex items-center gap-1.5 font-medium">
                      <Dot tone={sig.tone} />
                      {st.label}
                      <span className="text-xs font-normal text-muted">{sig.text}</span>
                    </div>
                  )}
                  <Mono className="ml-3.5 text-muted">{d.host}</Mono>
                </Td>
                <Td>
                  <Chip tone={d.cooling ? "warn" : d.ok ? "ok" : "neutral"}>{d.cooling ? `冷却 ${fmtDur(d.cooling)}` : d.ok ? "可用" : "未使用"}</Chip>
                  {d.last_status && (
                    <div className="max-w-[220px] truncate text-xs text-muted" title={d.last_status}>
                      {d.last_status}
                    </div>
                  )}
                </Td>
                <Td className="text-right">{fmtNum(d.ok)}</Td>
                <Td className="text-right">{fmtNum(d.blocked)}</Td>
                <Td className="text-right">{fmtNum(d.errors)}</Td>
              </tr>
            ));
          })}
        </tbody>
      </Table>
      {results && (
        <ul className="mt-3 space-y-1 rounded-md bg-panel-2 p-3 text-[13px]">
          {results.map(r => (
            <li key={r.site + r.host} className="flex flex-wrap items-center gap-2">
              <Chip tone={r.ok ? "ok" : "err"}>{r.ok ? "通" : "不通"}</Chip>
              <span>{meta.label(r.site)}</span>
              <Mono>{r.host}</Mono>
              <span className="text-muted">{r.error || `HTTP ${r.status}${r.blocked ? "，被 CF 拦截" : ""}，${r.ms}ms`}</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function RecentLogs() {
  const { items } = useLogs();
  return (
    <Panel title="最近日志" actions={<Button size="sm" variant="ghost" onClick={() => navigate("logs")}>全部日志</Button>}>
      <LogLines items={items.slice(-80)} short className="h-64" />
    </Panel>
  );
}
