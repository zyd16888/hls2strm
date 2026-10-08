import { useQuery } from "@tanstack/react-query";
import { Pause, Play, RotateCw, Trash2, X, XCircle } from "lucide-react";
import { type ReactNode, useState } from "react";
import { ErrorText } from "@/components/common";
import { ask } from "@/components/confirm";
import { Button } from "@/components/ui/button";
import { Chip, EmptyRow, KV, Mono, Panel, Progress, Table, Td, Th } from "@/components/ui/data";
import { Check, Field, Input, Segmented, Select, Textarea } from "@/components/ui/form";
import { Sheet, SheetClose, SheetContent } from "@/components/ui/overlay";
import { api, type Job, type Task, type TaskStatus } from "@/lib/api";
import { fmtDur, fmtNum, fmtTime } from "@/lib/format";
import { JOB_STATUS, kindName, TASK_STATUS } from "@/lib/labels";
import { useJobs, useLibraries, useMeta, useRun, useStatus } from "@/lib/queries";
import { replaceParams, useRoute } from "@/lib/route";
import { cn } from "@/lib/utils";

type Kind = "list" | "videos" | "backfill" | "probe" | "verify" | "rewrite";

const KINDS: { value: Kind; label: string }[] = [
  { value: "list", label: "列表地址" },
  { value: "videos", label: "指定影片" },
  { value: "probe", label: "补源" },
  { value: "backfill", label: "补全详情" },
  { value: "verify", label: "核对输出" },
  { value: "rewrite", label: "重写输出" },
];

export const jobTotal = (j: Job) => Object.values(j.tasks).reduce((a, b) => a + (b ?? 0), 0);
export const jobFinished = (j: Job) => (j.tasks.done ?? 0) + (j.tasks.failed ?? 0) + (j.tasks.gone ?? 0) + (j.tasks.cancelled ?? 0);

function progressText(j: Job): string {
  let text = `${fmtNum(jobFinished(j))} / ${fmtNum(jobTotal(j))}`;
  const st = j.state;
  const n = (k: string) => fmtNum(Number(st[k] ?? 0));
  if (st.last_page) text += `，共 ${st.last_page} 页`;
  if (j.kind === "locate" && st.checked != null) text += `，更新 ${n("updated")}，找不到 ${n("missing")}`;
  if (j.kind === "verify" && st.checked != null) {
    text += `，检查 ${n("checked")}，正常 ${n("ok")}，strm 缺 ${n("strm")}，nfo 缺 ${n("nfo")}，封面缺 ${n("cover")}`;
    if (j.params.repair) text += `；补写 ${n("repaired")}，补封面 ${n("covers_queued")}`;
    if (st.details_queued != null) text += `，抓详情 ${n("details_queued")}`;
    if (st.external_relocated) text += `；外部整理库找回位置 ${n("external_relocated")}`;
    if (st.external_rewritten) text += `，写回收件目录 ${n("external_rewritten")}`;
    if (st.external_missing) text += `，找不到 ${n("external_missing")}`;
    if (st.external_absent) text += `，外部整理目录不存在没补 ${n("external_absent")}`;
    if (st.external_empty) text += `，外部整理目录是空的没补 ${n("external_empty")}`;
    if (st.external_unavailable) text += `，外部整理目录不在或是空的没补 ${n("external_unavailable")}`; // 旧任务
    if (st.external_absent || st.external_empty || st.external_unavailable) text += "（先检查挂载；确认要补，核对时勾「外部整理目录不在或是空的也写回」）";
  }
  return text;
}

export default function Jobs() {
  const { params } = useRoute();
  const openId = Number(params.get("job")) || null;
  const setOpen = (id: number | null) => {
    const p = new URLSearchParams(params);
    if (id) p.set("job", String(id));
    else p.delete("job");
    replaceParams(p);
  };
  const { data: jobs = [] } = useJobs();
  const { data: status } = useStatus();
  const now = status?.now ?? Date.now() / 1000;
  const open = jobs.find(j => j.id === openId) ?? null;

  return (
    <>
      <NewJob />
      <Panel title="任务" bodyClassName="p-0">
        <Table>
          <thead>
            <tr>
              <Th className="pl-4">#</Th>
              <Th>名称</Th>
              <Th>状态</Th>
              <Th className="w-[260px]">进度</Th>
              <Th>子任务</Th>
              <Th>创建</Th>
              <Th className="pr-4">耗时</Th>
            </tr>
          </thead>
          <tbody>
            {jobs.map(j => {
              const [stText, stTone] = JOB_STATUS[j.status];
              const total = jobTotal(j);
              return (
                <tr
                  key={j.id}
                  onClick={() => setOpen(j.id)}
                  className={cn("cursor-pointer hover:bg-panel-2", openId === j.id && "bg-accent-soft/50")}
                >
                  <Td className="pl-4 text-muted">{j.id}</Td>
                  <Td>
                    <div className="font-medium">{j.name}</div>
                    <div className="text-xs text-muted">{kindName(j.kind)}</div>
                  </Td>
                  <Td>
                    <Chip tone={stTone}>{stText}</Chip>
                  </Td>
                  <Td>
                    <Progress value={total ? (jobFinished(j) * 100) / total : 0} tone={j.tasks.failed ? "warn" : "accent"} />
                    <div className="mt-1 line-clamp-2 text-xs text-muted">{progressText(j)}</div>
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {(Object.entries(j.tasks) as [TaskStatus, number][]).map(([st, n]) => (
                        <Chip key={st} tone={TASK_STATUS[st]?.[1]}>
                          {TASK_STATUS[st]?.[0] ?? st} {fmtNum(n)}
                        </Chip>
                      ))}
                    </div>
                  </Td>
                  <Td className="whitespace-nowrap text-[13px] text-muted">{fmtTime(j.created_at)}</Td>
                  <Td className="whitespace-nowrap pr-4 text-[13px] text-muted">{fmtDur((j.finished_at ?? now) - j.created_at)}</Td>
                </tr>
              );
            })}
            {jobs.length === 0 && <EmptyRow cols={7}>还没有任务。在上面新建一个，或者到「输出库与订阅」建订阅。</EmptyRow>}
          </tbody>
        </Table>
      </Panel>
      <Sheet open={!!open} onOpenChange={o => !o && setOpen(null)}>
        {open && (
          <SheetContent label={`任务 #${open.id}`} className="sm:max-w-3xl">
            <JobDetail job={open} now={now} onDeleted={() => setOpen(null)} />
          </SheetContent>
        )}
      </Sheet>
    </>
  );
}

function NewJob() {
  const meta = useMeta();
  const { data: libraries = [] } = useLibraries();
  const run = useRun();
  const [kind, setKind] = useState<Kind>("list");
  const [f, setF] = useState({ site: "jable", source: "", sort: "post_date", start_page: 1, end_page: 0, detail: true, urls: "", library_id: 1 });
  const [verify, setVerify] = useState({ repair: true, covers: true, details: false, force_external: false });
  const set = (patch: Partial<typeof f>) => setF(prev => ({ ...prev, ...patch }));
  const site = meta.site(f.site);
  const allLibs = kind === "rewrite" || kind === "probe" || kind === "verify";

  const create = async () => {
    const body: Record<string, unknown> = { kind };
    if (kind === "list") Object.assign(body, { site: f.site, source: f.source, sort: f.sort, start_page: f.start_page || 1, end_page: f.end_page || 0, detail: f.detail });
    if (kind === "videos") Object.assign(body, { site: f.site, urls: f.urls });
    if (kind === "probe") body.site = f.site;
    if (kind === "verify")
      Object.assign(body, {
        repair: verify.repair,
        covers: verify.repair && verify.covers,
        detail: verify.repair && verify.details,
        force_external: verify.repair && verify.force_external,
      });
    if (kind !== "backfill" && f.library_id) body.library_id = f.library_id;
    await run(() => api.post<{ id: number }>("/api/jobs", body), { success: r => `已创建任务 #${r.id}`, invalidate: [["jobs"], ["status"]] });
  };

  const hint: Record<Kind, string> = {
    list: `一次性抓取某个列表并输出到所选的库。${site.hint ? site.hint + "。" : ""}同一番号已经在库里（别的站抓过）的，只给它加一个源。要定时跟进更新，用「输出库与订阅」里的订阅。`,
    videos: "抓取指定影片的详情并加入所选的库，比批量任务先执行。",
    backfill: "为所有还没有详情的影片排队抓详情，写入它们所在的各个库。",
    probe: "给库里的影片找备用源：按番号到所选站点逐部查找（每部一次请求），找到就挂成这部影片的另一个源，播放时原来的源不能用会自动换过去。某个站没有的影片，在「补源重查间隔」内不再重复查。",
    verify:
      "检查数据库里每条输出在磁盘上还在不在：strm 有没有、内容是不是当前的播放地址，nfo 和封面有没有。勾上「补回」会重新写 strm 和 nfo（不联网），封面先从别的库硬链接，没有再下载。没详情的影片不写 nfo，勾「抓详情」会给它们排队抓（按站点限速，外部整理库不抓）。外部整理库按 strm 内容在收件目录和外部整理目录里找，找到只更新记录的路径；两边都找不到才写回收件目录；外部整理目录不存在或是空的不补（多半是挂载出了问题）。",
    rewrite: "改了对外地址、播放模式、令牌或路径模板以后，用它重写已有的 strm 和 nfo（不联网），路径变了会搬动文件。",
  };

  return (
    <Panel title="新建任务">
      <Segmented value={kind} onChange={setKind} options={KINDS} className="mb-4 flex-wrap" />
      <div className="flex flex-wrap items-end gap-3">
        {(kind === "list" || kind === "videos" || kind === "probe") && (
          <Field label={kind === "probe" ? "到哪个站点找" : "站点"} className="w-36">
            <Select value={f.site} onChange={e => set({ site: e.target.value, sort: meta.site(e.target.value).default_sort })}>
              {Object.entries(meta.sites).map(([k, m]) => (
                <option key={k} value={k}>
                  {m.label}
                </option>
              ))}
            </Select>
          </Field>
        )}
        {kind !== "backfill" && (
          <Field label={kind === "probe" ? "给哪个库的影片找" : "输出库"} className="w-44">
            <Select value={f.library_id} onChange={e => set({ library_id: Number(e.target.value) })}>
              {allLibs && <option value={0}>{kind === "probe" ? "全部影片" : "全部库"}</option>}
              {libraries.map(l => (
                <option key={l.id} value={l.id}>
                  {l.name}
                </option>
              ))}
            </Select>
          </Field>
        )}
        {kind === "list" && (
          <>
            <Field label="列表地址（站点网址或路径）" className="min-w-[280px] flex-1">
              <Input value={f.source} onChange={e => set({ source: e.target.value })} list="job-presets" placeholder="站点上的列表路径，或直接粘贴列表页网址" />
              <datalist id="job-presets">
                {site.presets.map(p => (
                  <option key={p.source} value={p.source}>
                    {p.name}
                  </option>
                ))}
              </datalist>
            </Field>
            <Field label="排序" className="w-32">
              <Select value={f.sort} onChange={e => set({ sort: e.target.value })}>
                {Object.entries(site.sorts).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="起始页" className="w-20">
              <Input type="number" min={1} value={f.start_page} onChange={e => set({ start_page: Number(e.target.value) })} />
            </Field>
            <Field label="结束页（0 = 最后）" className="w-32">
              <Input type="number" min={0} value={f.end_page} onChange={e => set({ end_page: Number(e.target.value) })} />
            </Field>
            <Check checked={f.detail} onChange={v => set({ detail: v })} className="h-8">
              抓详情
            </Check>
          </>
        )}
        {kind === "verify" && (
          <div className="flex min-h-8 flex-wrap items-center gap-x-4 gap-y-1">
            <Check checked={verify.repair} onChange={v => setVerify(p => ({ ...p, repair: v }))}>
              发现问题就补回
            </Check>
            <Check checked={verify.covers} disabled={!verify.repair} onChange={v => setVerify(p => ({ ...p, covers: v }))}>
              补封面（要下载）
            </Check>
            <Check checked={verify.details} disabled={!verify.repair} onChange={v => setVerify(p => ({ ...p, details: v }))}>
              没详情的抓详情（要联网）
            </Check>
            <Check checked={verify.force_external} disabled={!verify.repair} onChange={v => setVerify(p => ({ ...p, force_external: v }))}>
              外部整理目录不在或是空的也写回
            </Check>
          </div>
        )}
        <Button variant="primary" onClick={create}>
          创建任务
        </Button>
      </div>
      {kind === "videos" && (
        <Field label="影片网址或站内 key，每行一个（网址按域名认站点；只写 key 的用上面选的站点）" className="mt-3">
          <Textarea value={f.urls} onChange={e => set({ urls: e.target.value })} placeholder={"https://jable.tv/videos/ipzz-983/\nsone-001"} />
        </Field>
      )}
      <p className="mt-3 max-w-[90ch] text-[13px] leading-relaxed text-muted">{hint[kind]}</p>
    </Panel>
  );
}

const TASK_FILTERS: { value: "" | TaskStatus; label: string }[] = [
  { value: "", label: "全部" },
  { value: "failed", label: "失败" },
  { value: "pending", label: "待处理" },
  { value: "running", label: "运行中" },
  { value: "done", label: "完成" },
  { value: "gone", label: "下架" },
  { value: "cancelled", label: "已取消" },
];

function JobDetail({ job, now, onDeleted }: { job: Job; now: number; onDeleted: () => void }) {
  const run = useRun();
  const meta = useMeta();
  const [filter, setFilter] = useState<"" | TaskStatus>(job.tasks.failed ? "failed" : "");
  const { data: tasks = [] } = useQuery({
    queryKey: ["tasks", job.id, filter],
    queryFn: () => api.get<Task[]>(`/api/jobs/${job.id}/tasks?status=${filter}&limit=300`),
    refetchInterval: job.status === "running" ? 3000 : false,
  });
  const [stText, stTone] = JOB_STATUS[job.status];
  const total = jobTotal(job);
  const act = (action: string, success: string) =>
    run(() => api.post(`/api/jobs/${job.id}/${action}`), { success, invalidate: [["jobs"], ["status"], ["tasks", job.id]] });
  const active = job.status === "running" || job.status === "paused";

  return (
    <>
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-3">
        <span className="text-muted">#{job.id}</span>
        <h3 className="min-w-0 flex-1 truncate font-semibold" title={job.name}>
          {job.name}
        </h3>
        <Chip tone={stTone}>{stText}</Chip>
        <SheetClose className="rounded p-1 text-muted hover:bg-panel-2 hover:text-ink" aria-label="关闭">
          <X className="size-4" />
        </SheetClose>
      </header>
      <div className="scroll-thin min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
        <div className="flex flex-wrap gap-1.5">
          {job.status === "running" && (
            <Button size="sm" onClick={() => act("pause", "任务已暂停")}>
              <Pause />
              暂停
            </Button>
          )}
          {job.status === "paused" && (
            <Button size="sm" variant="primary" onClick={() => act("resume", "任务已恢复")}>
              <Play />
              恢复
            </Button>
          )}
          {(job.tasks.failed ?? 0) > 0 && (
            <Button size="sm" onClick={() => act("retry", "失败的子任务已重新排队")}>
              <RotateCw />
              重试失败的 {job.tasks.failed} 个
            </Button>
          )}
          {active ? (
            <Button
              size="sm"
              variant="danger"
              onClick={async () => {
                if (await ask(`取消任务 #${job.id}？`, "还没执行的子任务不再执行。", { confirmText: "取消任务", danger: true })) act("cancel", "任务已取消");
              }}
            >
              <XCircle />
              取消任务
            </Button>
          ) : (
            <Button
              size="sm"
              variant="danger"
              onClick={async () => {
                if (!(await ask(`删除任务 #${job.id}？`, "删掉任务和子任务的记录，已入库的影片和已生成的文件不受影响。", { confirmText: "删除", danger: true }))) return;
                const r = await run(() => api.del(`/api/jobs/${job.id}`), { success: "任务已删除", invalidate: [["jobs"]] });
                if (r) onDeleted();
              }}
            >
              <Trash2 />
              删除记录
            </Button>
          )}
        </div>

        <div>
          <Progress value={total ? (jobFinished(job) * 100) / total : 0} />
          <p className="mt-1.5 text-[13px] text-muted">{progressText(job)}</p>
        </div>

        <KV
          items={[
            ["类型", kindName(job.kind)],
            ["创建", fmtTime(job.created_at)],
            ["开始执行", job.started_at ? fmtTime(job.started_at) : "-"],
            ["结束", job.finished_at ? fmtTime(job.finished_at) : "-"],
            ["耗时", fmtDur((job.finished_at ?? now) - (job.started_at ?? job.created_at))],
            ...(job.error ? ([["错误", <ErrorText key="e" msg={job.error} />]] as [string, ReactNode][]) : []),
          ]}
        />

        <section>
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <h4 className="text-sm font-semibold">子任务</h4>
            <Segmented size="sm" value={filter} onChange={setFilter} options={TASK_FILTERS} className="ml-auto flex-wrap" />
          </div>
          <Table>
            <thead>
              <tr>
                <Th>ID</Th>
                <Th>目标</Th>
                <Th>状态</Th>
                <Th className="text-right">尝试</Th>
                <Th className="text-right">耗时</Th>
              </tr>
            </thead>
            <tbody>
              {tasks.map(t => (
                <tr key={t.id}>
                  <Td className="align-top text-muted">{t.id}</Td>
                  <Td className="align-top">
                    <div className="text-xs text-muted">
                      {kindName(t.kind)}
                      {t.site ? ` · ${meta.label(t.site)}` : ""}
                    </div>
                    <Mono>{t.target}</Mono>
                    {t.last_error && (
                      <div className="mt-1 text-xs">
                        <ErrorText msg={t.last_error} />
                      </div>
                    )}
                  </Td>
                  <Td className="align-top">
                    <Chip tone={TASK_STATUS[t.status]?.[1]}>{TASK_STATUS[t.status]?.[0] ?? t.status}</Chip>
                    {t.status === "pending" && t.next_run_at > now && <div className="mt-1 text-xs text-muted">{fmtTime(t.next_run_at)} 重试</div>}
                  </Td>
                  <Td className="text-right align-top">{t.attempts}</Td>
                  <Td className="whitespace-nowrap text-right align-top text-[13px] text-muted">{t.duration_ms != null ? `${(t.duration_ms / 1000).toFixed(1)}s` : ""}</Td>
                </tr>
              ))}
              {tasks.length === 0 && <EmptyRow cols={5}>没有{filter ? TASK_STATUS[filter][0] : ""}的子任务</EmptyRow>}
            </tbody>
          </Table>
          {tasks.length >= 300 && <p className="mt-2 text-xs text-muted">只显示最近更新的 300 个。</p>}
        </section>
      </div>
    </>
  );
}
