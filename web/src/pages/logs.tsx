import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Eraser, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { LogLines } from "@/components/log-lines";
import { Button } from "@/components/ui/button";
import { Dot } from "@/components/ui/data";
import { Check, Input, Segmented, Select } from "@/components/ui/form";
import { api, type LogArea } from "@/lib/api";
import { areaOf, clearLogs, LEVELS, useLogs } from "@/lib/logs";
import { useRun } from "@/lib/queries";
import { replaceParams, useRoute } from "@/lib/route";

const AREAS: { value: "" | LogArea; label: string; title: string }[] = [
  { value: "", label: "全部", title: "所有日志按时间排" },
  { value: "play", label: "播放", title: "播放请求、取地址、中转、线路测速、连通性检测" },
  { value: "task", label: "任务", title: "抓取列表和详情、补源、核对、归并、订阅调度等后台任务" },
  { value: "system", label: "系统", title: "启动、设置和接口操作、登录等" },
];

interface Level {
  level: string;
  default: string;
}

export default function Logs() {
  const { items, connected } = useLogs();
  const { params } = useRoute();
  const area = (AREAS.find(a => a.value === params.get("area"))?.value ?? "") as "" | LogArea;
  const setArea = (v: "" | LogArea) => {
    const p = new URLSearchParams(params);
    if (v) p.set("area", v);
    else p.delete("area");
    replaceParams(p);
  };
  const [show, setShow] = useState("INFO");
  const [kw, setKw] = useState("");
  const [follow, setFollow] = useState(true);
  const run = useRun();
  const qc = useQueryClient();
  const { data: level } = useQuery({ queryKey: ["log-level"], queryFn: ({ signal }) => api.get<Level>("/api/logs/level", signal) });

  const setLevel = async (lv: string) => {
    const r = await run(() => api.put<Level>("/api/logs/level", { level: lv }), { invalidate: [] });
    if (!r) return;
    qc.setQueryData(["log-level"], r);
    if (r.level === "DEBUG") setShow(""); // 记了 DEBUG 就一起显示出来
  };

  const counts = useMemo(() => {
    const c: Record<string, number> = { "": items.length };
    for (const l of items) c[areaOf(l)] = (c[areaOf(l)] ?? 0) + 1;
    return c;
  }, [items]);
  const shown = useMemo(() => {
    const min = LEVELS[show] ?? 0;
    const k = kw.trim().toLowerCase();
    return items.filter(l => (!area || areaOf(l) === area) && (LEVELS[l.level] ?? 0) >= min
      && (!k || l.msg.toLowerCase().includes(k) || l.name.includes(k)));
  }, [items, area, show, kw]);

  return (
    <div className="flex h-[calc(100vh-88px)] min-h-[420px] flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          value={area}
          onChange={setArea}
          options={AREAS.map(a => ({ value: a.value, title: a.title, label: <>{a.label} <span className="tabular-nums text-muted">{counts[a.value] ?? 0}</span></> }))}
        />
        <Select value={show} onChange={e => setShow(e.target.value)} aria-label="显示哪些级别" className="w-40">
          <option value="">显示全部级别</option>
          <option value="INFO">显示 INFO 及以上</option>
          <option value="WARNING">显示 WARNING 及以上</option>
          <option value="ERROR">只显示 ERROR</option>
        </Select>
        <Select
          value={level?.level ?? "INFO"}
          onChange={e => setLevel(e.target.value)}
          aria-label="服务端记哪些日志"
          title={`服务端记哪些日志；DEBUG 会记下每个请求。不保存，重启后回到 ${level?.default ?? "INFO"}`}
          className="w-48"
        >
          <option value="DEBUG">记录 DEBUG（每个请求）</option>
          <option value="INFO">记录 INFO</option>
          <option value="WARNING">记录 WARNING</option>
        </Select>
        <div className="relative min-w-[200px] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input value={kw} onChange={e => setKw(e.target.value)} placeholder="过滤：番号、站点、模块名…" className="pl-8" aria-label="过滤关键字" />
        </div>
        <Check checked={follow} onChange={setFollow}>
          自动滚到最新
        </Check>
        <span className="flex items-center gap-1.5 text-[13px] text-muted" title={connected ? "实时推送中" : "连接断开，3 秒后重连"}>
          <Dot tone={connected ? "ok" : "err"} pulse={connected} />
          {connected ? "实时" : "断开"}
        </span>
        <Button size="sm" variant="ghost" onClick={clearLogs} title="只清空这个页面上显示的">
          <Eraser />
          清空
        </Button>
      </div>
      <LogLines items={shown} follow={follow} className="min-h-0 flex-1 rounded-lg border border-line bg-panel" />
      <p className="text-xs text-muted">
        显示 {shown.length} / {items.length} 条（播放、任务、系统各留最近 3000 条，抓取刷屏不会挤掉播放日志；完整日志在数据目录的 logs/hls2strm.log）。
      </p>
    </div>
  );
}
