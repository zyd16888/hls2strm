import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, FolderSearch, Pencil, Undo2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Pager } from "@/components/common";
import { ask } from "@/components/confirm";
import { Button } from "@/components/ui/button";
import { Chip, Code, EmptyRow, Mono, Notice, Panel, Table, Td, Th } from "@/components/ui/data";
import { Check, Field, Input, Select } from "@/components/ui/form";
import { api, type ChangeSet, type Job, type MissingOutput, type Page, type PrefixPreview, type ScanSummary, type StrmFile } from "@/lib/api";
import { fmtNum, fmtTime } from "@/lib/format";
import { STRM_KINDS } from "@/lib/labels";
import { useLibraries, useRun, useStatus } from "@/lib/queries";

const kindTone = (k: string) => (k === "ours" ? "ok" : k === "invalid" ? "err" : "neutral");

export default function Strm() {
  const { data: status } = useStatus();
  const run = useRun();
  const qc = useQueryClient();
  const [dir, setDir] = useState("");
  const [scanId, setScanId] = useState<number | null>(null);
  const { data: scans = [] } = useQuery({
    queryKey: ["scans"],
    queryFn: ({ signal }) => api.get<Job[]>("/api/strm/scans", signal),
    refetchInterval: q => ((q.state.data ?? []).some(j => j.status === "running") ? 2000 : false),
  });
  useEffect(() => {
    if (scanId == null && scans.length) setScanId(scans[0].id);
  }, [scans, scanId]);
  const scan = scans.find(j => j.id === scanId);
  const { data: summary } = useQuery({
    queryKey: ["scan-summary", scanId, scan?.status],
    queryFn: ({ signal }) => api.get<ScanSummary>(`/api/strm/scans/${scanId}/summary`, signal),
    enabled: !!scanId,
  });

  const startScan = async () => {
    const r = await run(() => api.post<{ job_id: number }>("/api/strm/scan", { dir }), {
      success: r => `已开始扫描（任务 #${r.job_id}）`,
      invalidate: [["scans"], ["jobs"]],
    });
    if (r) setScanId(r.job_id);
  };

  const [prefixOld, setPrefixOld] = useState("");
  const [filePrefix, setFilePrefix] = useState("");
  const refreshScan = () => {
    qc.invalidateQueries({ queryKey: ["scan-summary"] });
    qc.invalidateQueries({ queryKey: ["strm-files"] });
    qc.invalidateQueries({ queryKey: ["strm-changes"] });
  };

  return (
    <>
      <Panel title="扫描 strm">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="目录（留空 = 输出根目录；也可以是别的工具生成 strm 的目录）" className="min-w-[300px] flex-1">
            <Input value={dir} onChange={e => setDir(e.target.value)} placeholder={status?.output_dir} />
          </Field>
          <Button variant="primary" onClick={startScan}>
            <FolderSearch />
            开始扫描
          </Button>
          <Field label="看哪次扫描" className="w-[340px] max-w-full">
            <Select value={scanId ?? ""} onChange={e => setScanId(Number(e.target.value))} disabled={!scans.length}>
              {scans.length === 0 && <option value="">还没扫描过</option>}
              {scans.map(j => (
                <option key={j.id} value={j.id}>
                  #{j.id} {String(j.params.dir ?? "")} · {fmtTime(j.created_at)}
                  {j.status === "running" ? "（扫描中）" : ""}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        {scan?.status === "running" && <p className="mt-3 text-sm text-warn">正在扫描，完成后自动显示结果…</p>}
        {summary && <Summary summary={summary} onPrefix={setPrefixOld} onFilter={setFilePrefix} />}
      </Panel>

      {summary && scanId && (
        <>
          <div className="grid gap-4 xl:grid-cols-2">
            <Adopt scanId={scanId} />
            <PrefixChange scanId={scanId} old={prefixOld} setOld={setPrefixOld} onApplied={refreshScan} />
          </div>
          <Files scanId={scanId} dir={String(summary.job.params.dir ?? "")} prefix={filePrefix} setPrefix={setFilePrefix} />
        </>
      )}
    </>
  );
}

function Summary({ summary, onPrefix, onFilter }: { summary: ScanSummary; onPrefix: (p: string) => void; onFilter: (p: string) => void }) {
  const [showMissing, setShowMissing] = useState(false);
  const missingCount = Number(summary.job.state.missing ?? 0);
  const { data: missing } = useQuery({
    queryKey: ["strm-missing", summary.job.id],
    queryFn: ({ signal }) => api.get<Page<MissingOutput>>(`/api/strm/scans/${summary.job.id}/missing?size=200`, signal),
    enabled: showMissing,
  });
  return (
    <div className="mt-4 space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {Object.entries(summary.kinds).map(([k, v]) => (
          <Chip key={k} tone={kindTone(k)}>
            {STRM_KINDS[k] ?? k} {fmtNum(v.total)}
            {v.managed ? `（已纳管 ${fmtNum(v.managed)}）` : ""}
          </Chip>
        ))}
        {summary.adoptable > 0 && (
          <Chip tone="warn">
            可纳管 {fmtNum(summary.adoptable)}
            {summary.adoptable_unknown ? `（其中库里没有 ${fmtNum(summary.adoptable_unknown)}）` : ""}
          </Chip>
        )}
        {missingCount > 0 && (
          <button type="button" onClick={() => setShowMissing(v => !v)}>
            <Chip tone="err" className="cursor-pointer underline-offset-2 hover:underline">
              库里有记录但文件缺失 {fmtNum(missingCount)}
            </Chip>
          </button>
        )}
      </div>
      {showMissing && (
        <div className="rounded-md border border-line p-3">
          <div className="mb-2 flex items-center gap-2">
            <h4 className="text-sm font-semibold">库里有记录但文件缺失</h4>
            <span className="text-xs text-muted">对相应的库执行「核对并补回」或「重写输出」即可重新生成。</span>
            <Button size="icon-sm" variant="ghost" className="ml-auto" onClick={() => setShowMissing(false)} aria-label="收起">
              <X />
            </Button>
          </div>
          <Table>
            <thead>
              <tr>
                <Th>影片</Th>
                <Th>输出库</Th>
                <Th>应在的位置</Th>
              </tr>
            </thead>
            <tbody>
              {(missing?.items ?? []).map(m => (
                <tr key={m.strm_path}>
                  <Td>
                    <Code>{m.slug}</Code>
                  </Td>
                  <Td className="text-[13px]">{m.library_name}</Td>
                  <Td>
                    <Mono>{m.strm_path}</Mono>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
      <Table>
        <thead>
          <tr>
            <Th>地址前缀</Th>
            <Th className="text-right">文件</Th>
            <Th className="text-right">已纳管</Th>
            <Th>示例</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {summary.prefixes.map(px => (
            <tr key={px.prefix}>
              <Td>
                <Mono>{px.prefix || "（没有前缀）"}</Mono>
                {px.prefix && px.prefix === summary.public_base_url && (
                  <Chip tone="info" className="ml-1.5">
                    本服务的对外地址
                  </Chip>
                )}
              </Td>
              <Td className="text-right">{fmtNum(px.n)}</Td>
              <Td className="text-right">{fmtNum(px.managed)}</Td>
              <Td className="max-w-[360px]">
                <Mono className="block truncate text-muted" title={px.sample_url}>
                  {px.sample_url}
                </Mono>
              </Td>
              <Td className="whitespace-nowrap text-right">
                <Button size="sm" variant="ghost" onClick={() => onFilter(px.prefix)}>
                  <Eye />
                  只看这些
                </Button>
                <Button size="sm" variant="ghost" onClick={() => onPrefix(px.prefix)}>
                  <Pencil />
                  改前缀
                </Button>
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

function Adopt({ scanId }: { scanId: number }) {
  const { data: libs = [] } = useLibraries();
  const run = useRun();
  const [f, setF] = useState({ library_id: 0, kinds: ["ours", "cdn"], fetch_missing: true, prefix: "" });
  const toggleKind = (k: string) => setF(p => ({ ...p, kinds: p.kinds.includes(k) ? p.kinds.filter(x => x !== k) : [...p.kinds, k] }));
  const start = async () => {
    if (f.kinds.includes("named") && !(await ask("纳管「文件名识别」的文件？", "这些文件的内容会被改成本服务的地址。确认它们都是对应站点的影片（不是 115、alist 里同番号的其他片源）再继续。", { confirmText: "继续纳管" })))
      return;
    await run(() => api.post<{ job_id: number }>("/api/strm/adopt", { scan_id: scanId, ...f, library_id: f.library_id || null }), {
      success: r => `已创建纳管任务 #${r.job_id}，完成后重新扫描可以看到结果`,
      invalidate: [["jobs"]],
    });
  };
  return (
    <Panel title="纳管">
      <p className="mb-3 text-[13px] text-muted">把识别出的影片关联到输出库：文件按库的目录结构归位，内容改成本服务的地址，之后随重写、改前缀一起维护。</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="放进哪个库">
          <Select value={f.library_id} onChange={e => setF({ ...f, library_id: Number(e.target.value) })}>
            <option value={0}>按所在目录（不在库目录下的跳过）</option>
            {libs.map(l => (
              <option key={l.id} value={l.id}>
                全部放进「{l.name}」
              </option>
            ))}
          </Select>
        </Field>
        <Field label="只处理这个前缀（可留空）">
          <Input value={f.prefix} onChange={e => setF({ ...f, prefix: e.target.value })} />
        </Field>
      </div>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2">
        {(["ours", "cdn", "named"] as const).map(k => (
          <Check key={k} checked={f.kinds.includes(k)} onChange={() => toggleKind(k)}>
            {STRM_KINDS[k]}
          </Check>
        ))}
        <Check checked={f.fetch_missing} onChange={v => setF({ ...f, fetch_missing: v })}>
          库里没有的影片先抓详情
        </Check>
      </div>
      <p className="mt-2 text-xs text-muted">「文件名识别」可能是 115、alist 里同番号的其他片源，确认后再勾。</p>
      <Button variant="primary" className="mt-3" onClick={start} disabled={!f.kinds.length}>
        开始纳管
      </Button>
    </Panel>
  );
}

function PrefixChange({ scanId, old, setOld, onApplied }: { scanId: number; old: string; setOld: (v: string) => void; onApplied: () => void }) {
  const run = useRun();
  const [next, setNext] = useState("");
  const [preview, setPreview] = useState<PrefixPreview | null>(null);
  useEffect(() => setPreview(null), [old, next, scanId]);
  const { data: changes = [] } = useQuery({ queryKey: ["strm-changes"], queryFn: ({ signal }) => api.get<ChangeSet[]>("/api/strm/changes", signal) });

  const doPreview = async () => {
    const r = await run(() => api.post<PrefixPreview>("/api/strm/prefix/preview", { scan_id: scanId, old, new: next }), { invalidate: [] });
    if (r) setPreview(r);
  };
  const apply = async () => {
    if (!preview || !(await ask(`把 ${fmtNum(preview.count)} 个 strm 的前缀改掉？`, <><Mono>{old}</Mono> 改成 <Mono>{next}</Mono>。改动会记录下来，可以回滚。</>, { confirmText: "改前缀" }))) return;
    const r = await run(() => api.post<{ job_id: number }>("/api/strm/prefix/apply", { scan_id: scanId, old, new: next }), {
      success: r => `已创建改前缀任务 #${r.job_id}`,
      invalidate: [["jobs"], ["strm-changes"]],
    });
    if (r) {
      setPreview(null);
      setTimeout(onApplied, 1500);
    }
  };

  return (
    <Panel title="批量改前缀">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="旧前缀">
          <Input value={old} onChange={e => setOld(e.target.value)} placeholder="http://192.168.1.10:8080" />
        </Field>
        <Field label="新前缀">
          <Input value={next} onChange={e => setNext(e.target.value)} placeholder="https://strm.example.com" />
        </Field>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button onClick={doPreview} disabled={!old || !next}>
          预览
        </Button>
        <Button variant="primary" onClick={apply} disabled={!preview?.count}>
          执行
        </Button>
        {preview && (
          <span className="text-[13px]">
            命中 <b>{fmtNum(preview.count)}</b> 个文件，其中已纳管 {fmtNum(preview.managed)}
          </span>
        )}
      </div>
      {preview?.updates_setting && (
        <div className="mt-3">
          <Notice tone="warn">旧前缀就是本服务的对外地址，执行后设置里的对外地址会同步改成新前缀。</Notice>
        </div>
      )}
      {preview && preview.samples.length > 0 && (
        <ul className="mt-3 space-y-1.5 rounded-md bg-panel-2 p-3">
          {preview.samples.map(x => (
            <li key={x.path} className="text-xs">
              <Mono className="block truncate text-muted" title={x.old}>
                {x.old}
              </Mono>
              <Mono className="block truncate" title={x.new}>
                → {x.new}
              </Mono>
            </li>
          ))}
        </ul>
      )}
      <h4 className="mb-1 mt-5 text-sm font-semibold">改动记录</h4>
      <Table>
        <thead>
          <tr>
            <Th>#</Th>
            <Th>旧 → 新</Th>
            <Th className="text-right">文件</Th>
            <Th className="text-right">已回滚</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {changes.map(cs => (
            <tr key={cs.change_set}>
              <Td className="text-muted">{cs.change_set}</Td>
              <Td>
                <Mono className="block">{cs.params.old}</Mono>
                <Mono className="block text-muted">→ {cs.params.new}</Mono>
              </Td>
              <Td className="text-right">{fmtNum(cs.files)}</Td>
              <Td className="text-right">{fmtNum(cs.reverted)}</Td>
              <Td className="text-right">
                {cs.reverted < cs.files && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={async () => {
                      if (!(await ask(`回滚改动 #${cs.change_set}？`, "已经被别处改过的文件会跳过。", { confirmText: "回滚" }))) return;
                      const r = await run(() => api.post<{ job_id: number }>(`/api/strm/changes/${cs.change_set}/revert`), {
                        success: r => `已创建回滚任务 #${r.job_id}`,
                        invalidate: [["jobs"], ["strm-changes"]],
                      });
                      if (r) setTimeout(onApplied, 1500);
                    }}
                  >
                    <Undo2 />
                    回滚
                  </Button>
                )}
              </Td>
            </tr>
          ))}
          {changes.length === 0 && <EmptyRow cols={5}>还没有改过前缀</EmptyRow>}
        </tbody>
      </Table>
    </Panel>
  );
}

function Files({ scanId, dir, prefix, setPrefix }: { scanId: number; dir: string; prefix: string; setPrefix: (p: string) => void }) {
  // 文件路径去掉扫描目录这段，截断时留下的是文件名
  const rel = (path: string) => (dir && path.startsWith(dir) ? path.slice(dir.length).replace(/^[\\/]/, "") : path);
  const [f, setF] = useState({ kind: "", managed: "", q: "", page: 1, size: 50 });
  const [q, setQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setF(p => (p.q === q ? p : { ...p, q, page: 1 })), 400);
    return () => clearTimeout(t);
  }, [q]);
  useEffect(() => setF(p => ({ ...p, page: 1 })), [prefix, scanId]);
  const params = new URLSearchParams({ kind: f.kind, managed: f.managed, prefix, q: f.q, page: String(f.page), size: String(f.size) });
  const { data } = useQuery({
    queryKey: ["strm-files", scanId, params.toString()],
    queryFn: ({ signal }) => api.get<Page<StrmFile>>(`/api/strm/scans/${scanId}/files?${params}`, signal),
    placeholderData: keepPreviousData,
  });
  return (
    <Panel title="文件" bodyClassName="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={f.kind} onChange={e => setF({ ...f, kind: e.target.value, page: 1 })} aria-label="类型" className="w-32">
          <option value="">全部类型</option>
          {Object.entries(STRM_KINDS).map(([k, n]) => (
            <option key={k} value={k}>
              {n}
            </option>
          ))}
        </Select>
        <Select value={f.managed} onChange={e => setF({ ...f, managed: e.target.value, page: 1 })} aria-label="纳管状态" className="w-28">
          <option value="">纳管状态</option>
          <option value="1">已纳管</option>
          <option value="0">没纳管</option>
        </Select>
        {prefix && (
          <span className="inline-flex h-7 items-center gap-1 rounded-md border border-accent/30 bg-accent-soft pl-2.5 pr-1 text-[13px] text-accent">
            前缀 {prefix}
            <button type="button" onClick={() => setPrefix("")} className="rounded p-0.5 hover:bg-accent/15" aria-label="去掉前缀条件">
              <X className="size-3.5" />
            </button>
          </span>
        )}
        <Input value={q} onChange={e => setQ(e.target.value)} placeholder="路径、地址、番号、备注" className="min-w-[200px] flex-1" />
      </div>
      <Table>
        <thead>
          <tr>
            <Th>文件（相对扫描目录）</Th>
            <Th>类型</Th>
            <Th>番号</Th>
            <Th>状态</Th>
            <Th>内容</Th>
          </tr>
        </thead>
        <tbody>
          {(data?.items ?? []).map(file => (
            <tr key={file.path}>
              <Td className="max-w-[380px]">
                <Mono className="block truncate" title={file.path}>
                  {rel(file.path)}
                </Mono>
              </Td>
              <Td className="whitespace-nowrap">
                <Chip tone={kindTone(file.kind)}>{STRM_KINDS[file.kind] ?? file.kind}</Chip>
                {!!file.expired && (
                  <Chip tone="err" className="ml-1">
                    已过期
                  </Chip>
                )}
              </Td>
              <Td>{file.slug ? <Code>{file.slug}</Code> : null}</Td>
              <Td>
                <Chip tone={file.managed ? "ok" : "neutral"}>{file.managed ? "已纳管" : file.video_id ? "库里有" : file.slug ? "库里没有" : "-"}</Chip>
                {file.note && <div className="mt-0.5 text-xs text-muted">{file.note}</div>}
              </Td>
              <Td className="max-w-[380px]">
                <Mono className="block truncate text-muted" title={file.url}>
                  {file.url}
                </Mono>
              </Td>
            </tr>
          ))}
          {(data?.items ?? []).length === 0 && <EmptyRow cols={5}>没有文件</EmptyRow>}
        </tbody>
      </Table>
      <Pager page={f.page} size={f.size} total={data?.total ?? 0} onPage={page => setF({ ...f, page })} onSize={size => setF({ ...f, size, page: 1 })} />
    </Panel>
  );
}
