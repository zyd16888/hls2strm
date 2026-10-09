import { useQuery } from "@tanstack/react-query";
import { MoreHorizontal, Pencil, Plus } from "lucide-react";
import { useState } from "react";
import { ask, confirm } from "@/components/confirm";
import { Button } from "@/components/ui/button";
import { Chip, EmptyRow, Help, Mono, Panel, Table, Td, Th } from "@/components/ui/data";
import { Check, Field, Input, Segmented, Select, Switch } from "@/components/ui/form";
import { Dialog, DialogContent, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "@/components/ui/overlay";
import { rewriteAll, runSubscription, verifyLibrary } from "@/lib/actions";
import { api, type FacetItem, type FacetField, type Library, type Subscription } from "@/lib/api";
import { fmtNum, fmtTime } from "@/lib/format";
import { useLibraries, useMeta, useRun, useStatus, useSubscriptions } from "@/lib/queries";
import { VERSION_STYLES } from "@/lib/labels";
import { navigate } from "@/lib/route";
import { subState } from "./overview";

export default function Libraries() {
  return (
    <>
      <LibrariesPanel />
      <SubscriptionsPanel />
    </>
  );
}

// ---- 输出库 ----

function LibrariesPanel() {
  const { data: libs = [] } = useLibraries(true);
  const { data: status } = useStatus();
  const run = useRun();
  const [editing, setEditing] = useState<Library | "new" | null>(null);
  const names = (ids: number[]) => ids.map(id => libs.find(l => l.id === id)?.name ?? `#${id}`).join("、");
  const done = { invalidate: [["libraries"], ["status"], ["jobs"]] };

  const remove = async (l: Library) => {
    const what = l.external_dir
      ? `${fmtNum(l.videos)} 部影片的 strm（外部整理库只删 strm，nfo 和图片留给外部工具）`
      : `目录下本服务生成的 ${fmtNum(l.videos)} 部影片的文件`;
    const choice = await confirm({
      title: `删除输出库「${l.name}」`,
      body: <p>要不要同时删掉{what}？</p>,
      choices: [
        { value: "keep", label: "只删记录，保留文件" },
        { value: "files", label: "连文件一起删", variant: "danger" },
      ],
    });
    if (!choice) return;
    await run(() => api.del<{ job_id: number }>(`/api/libraries/${l.id}?delete_files=${choice === "files"}`), {
      success: r => `已排队删除任务 #${r.job_id}`,
      ...done,
    });
  };

  return (
    <Panel
      title="输出库"
      actions={
        <>
          <span className="hidden text-[13px] text-muted md:inline">
            输出根目录 <Mono>{status?.output_dir}</Mono>
          </span>
          <Button size="sm" variant="primary" onClick={() => setEditing("new")}>
            <Plus />
            新建输出库
          </Button>
        </>
      }
      bodyClassName="px-4 py-0"
    >
      <Table>
        <thead>
          <tr>
            <Th>名称</Th>
            <Th>目录</Th>
            <Th>路径模板</Th>
            <Th>自动归库</Th>
            <Th className="text-right">影片</Th>
            <Th className="text-right">订阅</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {libs.map(l => (
            <tr key={l.id}>
              <Td>
                <button type="button" className="font-medium hover:text-accent" onClick={() => navigate("videos", { library: String(l.id) })} title="在影片库里看这个库">
                  {l.name}
                </button>
              </Td>
              <Td>
                <Mono>{l.root}</Mono>
                {l.external_dir && (
                  <div className="mt-1 flex items-center gap-1.5">
                    <Chip tone="info">外部整理</Chip>
                    <Mono className="text-muted">{l.external_root}</Mono>
                  </div>
                )}
                {l.versions && (
                  <div className="mt-1">
                    <Chip tone="info" title={VERSION_STYLES[l.versions]}>
                      多画质版本
                    </Chip>
                  </div>
                )}
              </Td>
              <Td>
                <Mono className={l.path_template ? "" : "text-muted"}>{l.path_template || "默认"}</Mono>
              </Td>
              <Td className="text-[13px]">
                {l.rule_text && <div>规则：{l.rule_text}</div>}
                {l.sources.length > 0 && <div>来源：{names(l.sources)}</div>}
                {l.excludes.length > 0 && <div>排除：{names(l.excludes)}</div>}
                {!l.rule_text && !l.sources.length && !l.excludes.length && <span className="text-muted">-</span>}
                {l.pending > 0 && l.excludes.length > 0 && <Chip tone="warn">等待归入 {fmtNum(l.pending)}</Chip>}
              </Td>
              <Td className="text-right">
                {fmtNum(l.videos)}
                {l.missing > 0 && (
                  <div>
                    <Chip tone="err" title="数据库里有记录、磁盘上找不到 strm">
                      文件缺失 {fmtNum(l.missing)}
                    </Chip>
                  </div>
                )}
              </Td>
              <Td className="text-right">{l.subscriptions}</Td>
              <Td className="whitespace-nowrap text-right">
                <Button size="sm" variant="ghost" onClick={() => setEditing(l)}>
                  <Pencil />
                  编辑
                </Button>
                <Menu>
                  <MenuTrigger asChild>
                    <Button size="icon-sm" variant="ghost" aria-label="更多操作">
                      <MoreHorizontal />
                    </Button>
                  </MenuTrigger>
                  <MenuContent>
                    <MenuItem onSelect={() => rewriteAll(run, l)}>重写输出</MenuItem>
                    <MenuItem onSelect={() => verifyLibrary(run, l)}>核对并补回</MenuItem>
                    {(l.rule || l.sources.length > 0 || l.excludes.length > 0) && (
                      <MenuItem onSelect={() => run(() => api.post<{ job_id: number }>(`/api/libraries/${l.id}/reclassify`), { success: r => `已排队重新归库 #${r.job_id}`, ...done })}>
                        重新归库
                      </MenuItem>
                    )}
                    {l.external_dir && (
                      <MenuItem onSelect={() => run(() => api.post<{ job_id: number }>(`/api/libraries/${l.id}/locate`), { success: r => `已排队同步位置 #${r.job_id}`, ...done })}>
                        同步位置
                      </MenuItem>
                    )}
                    {l.id !== 1 && (
                      <>
                        <MenuSeparator />
                        <MenuItem danger onSelect={() => remove(l)}>
                          删除输出库
                        </MenuItem>
                      </>
                    )}
                  </MenuContent>
                </Menu>
              </Td>
            </tr>
          ))}
          {libs.length === 0 && <EmptyRow cols={7}>加载中…</EmptyRow>}
        </tbody>
      </Table>
      <div className="py-3">
        <Help summary="输出库、外部整理库、来源库和排除库怎么用">
          <p>库目录不能互相嵌套。改目录或模板会自动排一个重写任务，把已有文件搬到新位置；改规则会自动排一个重新归库任务，不再符合规则的影片会从库里移除（只移除规则加入的，任务和订阅加入的保留）。组内多个值满足任一即可；分类、标签按站点上的名称或 slug 匹配。</p>
          <p>外部整理库：strm 只在影片第一次入库时写进库目录（不写 nfo、封面），之后由外部工具移走、改名、刮削，本服务不再在库目录补写，也不搬文件。「同步位置」按 strm 内容在库目录和外部整理目录里找回每部影片的文件；重写输出会先同步位置，再原地改 strm 内容；删除库时只删 strm。清空外部整理目录就改回由本服务管理。</p>
          <p>来源库、排除库：比如「其他」的来源选「全部」、排除「中文字幕」「无码」，就得到剩下的影片，每部只在一个库里，外部工具不会重复刮削。有排除库的库收到新片后先不写文件，等排除库的定时订阅都跑完一轮、确认它不属于排除库再写（最多晚一个订阅周期）。排除库的订阅请按「最近更新」排序。</p>
        </Help>
      </div>
      <Dialog open={!!editing} onOpenChange={o => !o && setEditing(null)}>
        {editing && <LibraryForm lib={editing === "new" ? null : editing} libs={libs} onDone={() => setEditing(null)} />}
      </Dialog>
    </Panel>
  );
}

const RULE_FIELDS: { key: "categories" | "tags" | "models" | "quality" | "keywords"; label: string; facet?: FacetField; placeholder?: string }[] = [
  { key: "categories", label: "分类（名称或 slug，逗号分隔）", facet: "categories", placeholder: "中文字幕" },
  { key: "tags", label: "标签", facet: "tags", placeholder: "巨乳, 人妻" },
  { key: "models", label: "女优（名字或 id）", facet: "models" },
  { key: "quality", label: "画质", facet: "quality", placeholder: "中文字幕" },
  { key: "keywords", label: "关键词（番号或标题，逗号分隔）", placeholder: "SONE-, IPZZ-" },
];

function LibraryForm({ lib, libs, onDone }: { lib: Library | null; libs: Library[]; onDone: () => void }) {
  const run = useRun();
  const join = (v: string[] | undefined) => (v ?? []).join(", ");
  const [f, setF] = useState({
    name: lib?.name ?? "",
    dir: lib?.dir ?? "",
    path_template: lib?.path_template ?? "",
    external_dir: lib?.external_dir ?? "",
    versions: lib?.versions ?? "",
    sources: lib?.sources ?? [],
    excludes: lib?.excludes ?? [],
  });
  const [useRule, setUseRule] = useState(!!lib?.rule);
  const [rule, setRule] = useState({
    categories: join(lib?.rule?.categories),
    tags: join(lib?.rule?.tags),
    models: join(lib?.rule?.models),
    quality: join(lib?.rule?.quality),
    keywords: join(lib?.rule?.keywords),
    match: lib?.rule?.match ?? "any",
  });
  const { data: facets } = useQuery({
    queryKey: ["facets-all"],
    queryFn: ({ signal }) => api.get<Record<FacetField, FacetItem[]>>("/api/facets", signal),
    enabled: useRule,
    staleTime: 60_000,
  });
  const others = libs.filter(x => x.id !== lib?.id);
  const toggle = (key: "sources" | "excludes", id: number) =>
    setF(p => ({ ...p, [key]: p[key].includes(id) ? p[key].filter(x => x !== id) : [...p[key], id] }));

  const save = async () => {
    const body = { ...f, rule: useRule ? rule : null };
    const r = await run(
      () =>
        lib
          ? api.put<Record<string, number | null>>(`/api/libraries/${lib.id}`, body)
          : api.post<Record<string, number | null>>("/api/libraries", body),
      {
        success: r => {
          const jobs = [
            r.rewrite_job_id && `重写 #${r.rewrite_job_id}`,
            r.reclassify_job_id && `重新归库 #${r.reclassify_job_id}`,
            r.locate_job_id && `同步位置 #${r.locate_job_id}`,
          ].filter(Boolean);
          return (lib ? "已保存" : "已新建输出库") + (jobs.length ? `，已排队：${jobs.join("、")}` : "");
        },
        invalidate: [["libraries"], ["jobs"], ["status"]],
      },
    );
    if (r) onDone();
  };

  return (
    <DialogContent
      className="max-w-2xl"
      title={lib ? `编辑输出库「${lib.name}」` : "新建输出库"}
      footer={
        <>
          <Button onClick={onDone}>取消</Button>
          <Button variant="primary" onClick={save} disabled={!f.name.trim() || !f.dir.trim()}>
            {lib ? "保存" : "新建"}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="名称">
          <Input value={f.name} onChange={e => setF({ ...f, name: e.target.value })} placeholder="中文字幕" autoFocus />
        </Field>
        <Field label="目录（相对输出根目录，或绝对路径）">
          <Input value={f.dir} onChange={e => setF({ ...f, dir: e.target.value })} placeholder="中文字幕" />
        </Field>
        <Field label="路径模板（留空用默认）" hint="可用 {slug} {code} {actor} {year}，必须包含 {slug}">
          <Input value={f.path_template} onChange={e => setF({ ...f, path_template: e.target.value })} placeholder="{actor}/{slug}" />
        </Field>
        <Field label="外部整理目录" hint="交给 mdcng 等工具移动、刮削时，填它的整理目标；留空由本服务管理">
          <Input value={f.external_dir} onChange={e => setF({ ...f, external_dir: e.target.value })} placeholder="/media/jable-mdc/中文字幕" />
        </Field>
        <Field
          label="多画质版本"
          hint="一部片有好几档画质时，在主 strm 旁边写每一档的 strm，在 Emby / fyms 里能选版本。外部整理库等外部工具整理好以后再写。改了会排一次重写"
          className="sm:col-span-2"
        >
          <Select value={f.versions} onChange={e => setF({ ...f, versions: e.target.value as Library["versions"] })}>
            {Object.entries(VERSION_STYLES).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      {others.length > 0 && (
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Field label="来源库：这些库里的影片自动归入本库">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {others.map(o => (
                <Check key={o.id} checked={f.sources.includes(o.id)} onChange={() => toggle("sources", o.id)}>
                  {o.name}
                </Check>
              ))}
            </div>
          </Field>
          <Field label="排除库：已在这些库里的影片本库不收">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {others.map(o => (
                <Check key={o.id} checked={f.excludes.includes(o.id)} onChange={() => toggle("excludes", o.id)}>
                  {o.name}
                </Check>
              ))}
            </div>
          </Field>
        </div>
      )}
      <div className="mt-5 rounded-md border border-line p-3">
        <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
          <Switch checked={useRule} onCheckedChange={setUseRule} />
          规则库：按条件自动归入（抓到详情后生效）
        </label>
        {useRule && (
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {RULE_FIELDS.map(rf => (
              <Field key={rf.key} label={rf.label}>
                <Input value={rule[rf.key]} onChange={e => setRule({ ...rule, [rf.key]: e.target.value })} list={rf.facet ? `facet-${rf.facet}` : undefined} placeholder={rf.placeholder} />
                {rf.facet && (
                  <datalist id={`facet-${rf.facet}`}>
                    {(facets?.[rf.facet] ?? []).map(x => (
                      <option key={x.item} value={x.name}>
                        {x.n} 部
                      </option>
                    ))}
                  </datalist>
                )}
              </Field>
            ))}
            <Field label="几组条件之间">
              <Segmented
                value={rule.match}
                onChange={v => setRule({ ...rule, match: v })}
                options={[
                  { value: "any", label: "满足任一组" },
                  { value: "all", label: "每组都满足" },
                ]}
              />
            </Field>
          </div>
        )}
      </div>
    </DialogContent>
  );
}

// ---- 订阅 ----

function SubscriptionsPanel() {
  const { data: subs = [] } = useSubscriptions(true);
  const meta = useMeta();
  const run = useRun();
  const [editing, setEditing] = useState<Subscription | "new" | null>(null);
  const done = { invalidate: [["subscriptions"], ["status"], ["jobs"]] };

  return (
    <Panel
      title="订阅"
      actions={
        <Button size="sm" variant="primary" onClick={() => setEditing("new")}>
          <Plus />
          新建订阅
        </Button>
      }
      bodyClassName="px-4 py-0"
    >
      <Table>
        <thead>
          <tr>
            <Th>名称 / 来源</Th>
            <Th>输出库</Th>
            <Th>周期</Th>
            <Th>增量规则</Th>
            <Th>状态</Th>
            <Th>上次运行</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {subs.map(sub => {
            const st = subState(sub);
            const sortName = sub.sort ? meta.site(sub.site).sorts[sub.sort] ?? sub.sort : "";
            return (
              <tr key={sub.id}>
                <Td>
                  <div className="font-medium">{sub.name}</div>
                  <div className="text-xs text-muted">
                    {meta.label(sub.site)} <Mono className="text-xs">{sub.source}</Mono>
                    {sortName && `，按${sortName}`}
                  </div>
                </Td>
                <Td className="text-[13px]">{sub.library_name}</Td>
                <Td className="whitespace-nowrap text-[13px]">{sub.interval ? `每 ${sub.interval} 分钟` : "手动"}</Td>
                <Td className="text-[13px] text-muted">
                  连续 {sub.stop_after_known} 部已在库就停，最多 {sub.max_pages} 页{sub.detail ? "" : "，不抓详情"}
                </Td>
                <Td className="whitespace-nowrap">
                  <Chip tone={st.tone}>{st.text}</Chip>
                  {sub.active_job_id && (
                    <button type="button" className="ml-1.5 text-xs text-accent hover:underline" onClick={() => navigate("jobs", { job: String(sub.active_job_id) })}>
                      #{sub.active_job_id}
                    </button>
                  )}
                </Td>
                <Td className="whitespace-nowrap text-[13px] text-muted">{sub.last_run_at ? fmtTime(sub.last_run_at) : "-"}</Td>
                <Td className="whitespace-nowrap text-right">
                  {!sub.listing_job_id &&
                    (sub.initialized ? (
                      <Button size="sm" onClick={() => runSubscription(run, sub, "incremental")}>
                        立即增量
                      </Button>
                    ) : (
                      <Button size="sm" variant="primary" onClick={() => runSubscription(run, sub, "full")}>
                        首轮全量
                      </Button>
                    ))}
                  <Menu>
                    <MenuTrigger asChild>
                      <Button size="icon-sm" variant="ghost" aria-label="更多操作">
                        <MoreHorizontal />
                      </Button>
                    </MenuTrigger>
                    <MenuContent>
                      <MenuItem onSelect={() => setEditing(sub)}>编辑</MenuItem>
                      {!sub.listing_job_id && sub.initialized ? <MenuItem onSelect={() => runSubscription(run, sub, "full")}>重跑全量</MenuItem> : null}
                      {!sub.listing_job_id && !sub.initialized ? (
                        <MenuItem
                          onSelect={async () => {
                            if (await ask(`订阅「${sub.name}」不跑首轮全量？`, "直接改为定时增量。库里已经用别的任务抓全时用它。", { confirmText: "标记首轮已完成" }))
                              run(() => api.post(`/api/subscriptions/${sub.id}/initialized`), { success: "已标记", ...done });
                          }}
                        >
                          标记首轮已完成
                        </MenuItem>
                      ) : null}
                      <MenuSeparator />
                      <MenuItem
                        danger
                        onSelect={async () => {
                          if (await ask(`删除订阅「${sub.name}」？`, "已输出的文件不受影响。", { confirmText: "删除", danger: true }))
                            run(() => api.del(`/api/subscriptions/${sub.id}`), { success: "订阅已删除", ...done });
                        }}
                      >
                        删除订阅
                      </MenuItem>
                    </MenuContent>
                  </Menu>
                </Td>
              </tr>
            );
          })}
          {subs.length === 0 && <EmptyRow cols={7}>还没有订阅。新建一个，按周期自动跟进站点上的更新。</EmptyRow>}
        </tbody>
      </Table>
      <p className="py-3 text-[13px] text-muted">
        首轮全量会翻完来源的全部页；之后按周期增量：从第 1 页往后翻，连续遇到这么多部已在该库里的影片就停。不勾「首轮全量」只跟进以后的更新。
      </p>
      <Dialog open={!!editing} onOpenChange={o => !o && setEditing(null)}>
        {editing && <SubscriptionForm sub={editing === "new" ? null : editing} onDone={() => setEditing(null)} />}
      </Dialog>
    </Panel>
  );
}

function SubscriptionForm({ sub, onDone }: { sub: Subscription | null; onDone: () => void }) {
  const meta = useMeta();
  const { data: libs = [] } = useLibraries();
  const run = useRun();
  const [f, setF] = useState({
    name: sub?.name ?? "",
    site: sub?.site ?? "jable",
    source: sub?.source ?? "",
    sort: sub?.sort ?? meta.site("jable").default_sort,
    library_id: sub?.library_id ?? 1,
    interval: sub?.interval ?? 60,
    stop_after_known: sub?.stop_after_known ?? 48,
    max_pages: sub?.max_pages ?? 20,
    detail: sub ? !!sub.detail : true,
    enabled: sub ? !!sub.enabled : true,
    initial_full: true,
  });
  const site = meta.site(f.site);
  const save = async () => {
    const r = await run(() => (sub ? api.put(`/api/subscriptions/${sub.id}`, f) : api.post("/api/subscriptions", f)), {
      success: sub ? "订阅已保存" : "订阅已新建",
      invalidate: [["subscriptions"], ["status"]],
    });
    if (r) onDone();
  };
  return (
    <DialogContent
      className="max-w-2xl"
      title={sub ? `编辑订阅「${sub.name}」` : "新建订阅"}
      footer={
        <>
          <Button onClick={onDone}>取消</Button>
          <Button variant="primary" onClick={save} disabled={!f.name.trim() || !f.source.trim()}>
            {sub ? "保存" : "新建"}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="名称">
          <Input value={f.name} onChange={e => setF({ ...f, name: e.target.value })} placeholder="中文字幕" autoFocus />
        </Field>
        <Field label="输出库">
          <Select value={f.library_id} onChange={e => setF({ ...f, library_id: Number(e.target.value) })}>
            {libs.map(l => (
              <option key={l.id} value={l.id}>
                {l.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="站点">
          <Select value={f.site} onChange={e => setF({ ...f, site: e.target.value, sort: meta.site(e.target.value).default_sort })}>
            {Object.entries(meta.sites).map(([k, m]) => (
              <option key={k} value={k}>
                {m.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="排序">
          <Select value={f.sort} onChange={e => setF({ ...f, sort: e.target.value })}>
            {Object.entries(site.sorts).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="列表地址" hint={site.hint} className="sm:col-span-2">
          <Input value={f.source} onChange={e => setF({ ...f, source: e.target.value })} list="sub-presets" placeholder="/categories/chinese-subtitle/" />
          <datalist id="sub-presets">
            {site.presets.map(p => (
              <option key={p.source} value={p.source}>
                {p.name}
              </option>
            ))}
          </datalist>
        </Field>
        <Field label="周期（分钟，0 = 只手动）">
          <Input type="number" min={0} value={f.interval} onChange={e => setF({ ...f, interval: Number(e.target.value) })} />
        </Field>
        <Field label="连续多少部已在库就停">
          <Input type="number" min={1} value={f.stop_after_known} onChange={e => setF({ ...f, stop_after_known: Number(e.target.value) })} />
        </Field>
        <Field label="单次最多页数">
          <Input type="number" min={1} value={f.max_pages} onChange={e => setF({ ...f, max_pages: Number(e.target.value) })} />
        </Field>
        <div className="flex flex-wrap items-end gap-x-5 gap-y-2 pb-1.5">
          <Check checked={f.detail} onChange={v => setF({ ...f, detail: v })}>
            抓详情
          </Check>
          <Check checked={f.enabled} onChange={v => setF({ ...f, enabled: v })}>
            启用
          </Check>
          {!sub && (
            <Check checked={f.initial_full} onChange={v => setF({ ...f, initial_full: v })}>
              首轮全量
            </Check>
          )}
        </div>
      </div>
    </DialogContent>
  );
}
