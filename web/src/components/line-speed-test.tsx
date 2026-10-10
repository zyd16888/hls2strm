import { Gauge, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, type LineSpeedEvent, type LineSpeedResult } from "@/lib/api";
import { Button } from "./ui/button";
import { Chip, Table, Td, Th } from "./ui/data";
import { Check, Select } from "./ui/form";

const STATUS = { waiting: "等待探测", ok: "可访问", failed: "失败", timeout: "超时", busy: "本服务排队超时", skipped: "跳过", cancelled: "已停止" };
const ms = (value?: number) => value == null ? "—" : `${value.toFixed(0)} ms`;

export function LineSpeedTest({ slug }: { slug: string }) {
  const request = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [items, setItems] = useState<LineSpeedResult[]>([]);
  const [error, setError] = useState("");
  const [includeDisabled, setIncludeDisabled] = useState(false);
  const [sort, setSort] = useState("line");
  const [sample, setSample] = useState(0);
  useEffect(() => () => request.current?.abort(), [slug]);

  const start = async () => {
    const controller = new AbortController();
    request.current = controller;
    setBusy(true); setItems([]); setError("");
    let completed = false;
    try {
      await api.stream<LineSpeedEvent>(`/api/videos/${slug}/speed-test`, { include_disabled: includeDisabled }, controller.signal, event => {
        if (event.event === "start") { setItems(event.items); setSample(event.sample_kb); }
        if (event.event === "result") setItems(previous => previous.map(item => item.key === event.item.key ? event.item : item));
        if (event.event === "done") completed = true;
      });
      if (!completed) throw new Error("测速连接中断，未完成的线路可重新探测");
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "测速失败");
      setItems(previous => previous.map(item => item.status === "waiting" ? { ...item, status: "cancelled" } : item));
    } finally {
      if (request.current === controller) { request.current = null; setBusy(false); }
    }
  };
  const rows = sort === "speed" ? [...items].sort((a, b) => (b.mbps ?? -1) - (a.mbps ?? -1)) : items;
  const finished = items.filter(item => item.status !== "waiting").length;
  return <section className="min-w-0 space-y-2" aria-label="影片线路测速">
    <div className="flex flex-wrap items-center gap-2">
      <h4 className="mr-auto text-sm font-semibold">全部线路测速</h4>
      <Button size="sm" onClick={start} disabled={busy}><Gauge className={busy ? "animate-pulse" : ""} />{busy ? "测速中…" : items.length ? "重新测速" : "开始测速"}</Button>
      {busy && <Button size="sm" onClick={() => request.current?.abort()}><X />停止</Button>}
    </div>
    <p className="text-xs leading-relaxed text-muted">并行检查这部影片各个源和线路的响应与媒体速度，不转码。测的是本服务到 CDN 的链路；受出口限制的源在外部客户端直连时可能无法播放。多线路并行下载会共享服务器带宽。</p>
    <div className="flex flex-wrap items-center gap-3 text-xs">
      <Check checked={includeDisabled} disabled={busy} onChange={setIncludeDisabled}>包含停用的站点和线路</Check>
      {items.length > 0 && <>
        <Select value={sort} onChange={event => setSort(event.target.value)} aria-label="测速结果排序"><option value="line">按线路顺序</option><option value="speed">按速度排序</option></Select>
        <span className="text-muted">完成 {finished}/{items.length} · 每条最多采样 {sample} KiB</span>
      </>}
      {busy && !items.length && <span className="text-muted">正在读取各源线路…</span>}
    </div>
    {error && <p role="alert" className="text-xs text-err">{error}</p>}
    {items.length > 0 && <Table className="text-xs"><thead><tr>
      <Th>源 / 线路</Th><Th>外部播放</Th><Th>解析 / 清单</Th><Th>媒体首字节</Th><Th>采样速度</Th><Th>采样</Th><Th>结果</Th>
    </tr></thead><tbody>{rows.map(item => <tr key={item.key}>
      <Td><div className="whitespace-nowrap font-medium">{item.label} {item.line}</div><div className="text-muted">{item.host || item.media_type || ""}</div></Td>
      <Td className="whitespace-nowrap">{item.mode || "—"}</Td>
      <Td className="whitespace-nowrap">{ms(item.resolve_ms)}<div className="text-muted">清单 {ms(item.manifest_ms)}</div></Td>
      <Td className="whitespace-nowrap">{ms(item.ttfb_ms)}</Td>
      <Td className="whitespace-nowrap font-mono">{item.mbps == null ? "—" : `${item.mbps.toFixed(2)} Mbps`}</Td>
      <Td className="whitespace-nowrap">{item.sample_bytes == null ? "—" : `${(item.sample_bytes/1024).toFixed(0)} KiB`}<div className="text-muted">{ms(item.download_ms)}</div></Td>
      <Td><Chip tone={item.status === "ok" ? "ok" : item.status === "failed" ? "err" : item.status === "timeout" || item.status === "busy" ? "warn" : "neutral"}>{STATUS[item.status]}{item.http_status ? ` · ${item.http_status}` : ""}</Chip>
        {item.total_ms != null && <div className="text-muted">总耗时 {ms(item.total_ms)}{item.queue_ms ? `，排队 ${ms(item.queue_ms)}` : ""}</div>}
        {item.error && <p className="mt-1 min-w-32 max-w-52 break-words text-muted">{item.error}</p>}
      </Td>
    </tr>)}</tbody></Table>}
  </section>;
}
