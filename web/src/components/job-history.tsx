import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type CrawlItem, type Job, type LogItem } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { useMeta } from "@/lib/queries";
import { LogLines } from "./log-lines";
import { Button } from "./ui/button";
import { Chip, EmptyRow, Table, Td, Th } from "./ui/data";
import { Segmented } from "./ui/form";

const itemFilters = [
  { value: "", label: "全部" }, { value: "new", label: "新增影片" },
  { value: "existing", label: "重复影片" }, { value: "excluded", label: "排除跳过" },
];

export function CrawlResults({ job }: { job: Job }) {
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(0);
  const meta = useMeta();
  const { data, error, isPending } = useQuery({
    queryKey: ["crawl-items", job.id, filter, page, job.status],
    queryFn: ({ signal }) => api.get<{ items: CrawlItem[]; total: number }>(`/api/jobs/${job.id}/items?status=${filter}&limit=50&offset=${page * 50}`, signal),
    refetchInterval: job.status === "running" ? 3000 : false,
  });
  const c = job.crawl;
  return <section className="space-y-2">
    <h4 className="text-sm font-semibold">列表抓取结果</h4>
    {c && <div className="flex flex-wrap gap-2">
      <Chip>已识别 {fmtNum(c.seen)} 部</Chip><Chip tone="ok">新增 {fmtNum(c.new)}</Chip>
      <Chip>重复 {fmtNum(c.existing)}</Chip><Chip tone="info">新加入输出库 {fmtNum(c.added)}</Chip>
      <Chip>排除跳过 {fmtNum(c.excluded)}</Chip>
    </div>}
    <p className="text-xs text-muted">同一影片在本任务只计一次。新增指首次进入影片库，重复指库里已有；已有影片也可能新加入此输出库。旧任务未记录的明细无法补算。</p>
    <Segmented size="sm" value={filter} onChange={v => { setFilter(v); setPage(0); }} options={itemFilters} className="flex-wrap" />
    {error && <p role="alert" className="text-xs text-err">{error.message}</p>}
    <div className="scroll-thin max-h-80 overflow-auto">
    <Table><thead><tr><Th>影片</Th><Th>来源 / 页码</Th><Th>结果</Th></tr></thead>
      <tbody>{data?.items.map(v => <tr key={v.video_id}>
        <Td><div className="font-mono">{v.slug}</div><div className="max-w-[360px] truncate text-xs text-muted" title={v.title}>{v.title}</div></Td>
        <Td className="whitespace-nowrap text-xs">{meta.label(v.site)} · 第 {v.page} 页</Td>
        <Td><div className="flex flex-wrap gap-1"><Chip tone={v.is_new ? "ok" : "neutral"}>{v.is_new ? "新增" : "重复"}</Chip>
          {!!v.excluded && <Chip tone="warn">排除跳过</Chip>}{!!v.added && <Chip tone="info">加入输出库</Chip>}</div></Td>
      </tr>)}{!data?.items.length && <EmptyRow cols={3}>{isPending ? "加载中…" : "暂无已记录的影片"}</EmptyRow>}</tbody>
    </Table>
    </div>
    <div className="flex items-center justify-end gap-2 text-xs text-muted">
      <span>共 {fmtNum(data?.total ?? 0)} 部 · 第 {page + 1} 页</span>
      <Button size="sm" disabled={!page} onClick={() => setPage(p => p - 1)}>上一页</Button>
      <Button size="sm" disabled={!data || (page + 1) * 50 >= data.total} onClick={() => setPage(p => p + 1)}>下一页</Button>
    </div>
  </section>;
}

export function JobLogs({ job, taskId, onClear }: { job: Job; taskId: number | null; onClear: () => void }) {
  const [before, setBefore] = useState(0);
  const box = useRef<HTMLElement>(null);
  const { data, error, isPending } = useQuery({
    queryKey: ["job-logs", job.id, taskId, before],
    queryFn: ({ signal }) => api.get<{ items: (LogItem & { task_id: number })[]; next_before: number | null }>(
      `/api/jobs/${job.id}/logs?before=${before}${taskId ? `&task_id=${taskId}` : ""}`, signal),
    // 终态之后也刷新当前页，接收结束时的最后一批日志。
    refetchInterval: before ? false : 3000,
  });
  useEffect(() => { if (taskId) box.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }); }, [taskId]);
  return <section ref={box} className="space-y-2">
    <div className="flex flex-wrap items-center gap-2">
      <h4 className="text-sm font-semibold">执行日志{taskId ? ` · 子任务 #${taskId}` : ""}</h4>
      {taskId && <Button size="sm" onClick={onClear}>全部子任务日志</Button>}
      <div className="ml-auto flex gap-1">
        <Button size="sm" disabled={!data?.next_before} onClick={() => setBefore(data!.next_before!)}>更早日志</Button>
        <Button size="sm" disabled={!before} onClick={() => setBefore(0)}>回到最新</Button>
      </div>
    </div>
    <p className="text-xs text-muted">保存最近 10,000 条执行日志，每页 200 条，刷新或重启后仍可查看。旧任务没有保存的日志无法追溯。</p>
    {error && <p role="alert" className="text-xs text-err">日志加载失败：{error.message}</p>}
    {isPending ? <p className="text-xs text-muted">加载日志…</p> : <LogLines items={(data?.items ?? []).map(l => ({ ...l, msg: `[#${l.task_id}] ${l.msg}` }))} follow={!before} className="h-64" />}
  </section>;
}
