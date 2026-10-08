import { useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Play, RefreshCw, Search, X } from "lucide-react";
import { useState } from "react";
import { api, type ProbeResult, type Source, type SourceLine, type Video } from "@/lib/api";
import { fmtClockDur, fmtNum, fmtTime } from "@/lib/format";
import { PROBE_STATUS, SUBTITLES } from "@/lib/labels";
import { useRun } from "@/lib/queries";
import { createStore } from "@/lib/store";
import { CopyButton, streamState } from "./common";
import { playVideo } from "./player";
import { Button } from "./ui/button";
import { Chip, Code, KV, Mono } from "./ui/data";
import { Sheet, SheetClose, SheetContent } from "./ui/overlay";

interface DetailState {
  video: Video;
  probe: ProbeResult[] | null;
}

const store = createStore<DetailState | null>(null);

export function openDetail(video: Video) {
  store.set({ video, probe: null });
}

export function closeDetail() {
  store.set(null);
}

export function DetailHost() {
  const st = store.use();
  return (
    <Sheet open={!!st} onOpenChange={open => !open && store.set(null)}>
      {st && (
        <SheetContent label={`影片 ${st.video.slug}`} className="sm:max-w-[720px]">
          <Detail key={st.video.slug} v={st.video} probe={st.probe} />
        </SheetContent>
      )}
    </Sheet>
  );
}

function Detail({ v, probe }: { v: Video; probe: ProbeResult[] | null }) {
  const run = useRun();
  const qc = useQueryClient();
  const [busy, setBusy] = useState<"" | "refresh" | "probe">("");

  const update = (nv: Video & { probe?: ProbeResult[] }) => {
    const { probe: results, ...video } = nv;
    store.set(prev => (prev ? { video: video as Video, probe: results ?? prev.probe } : prev));
    qc.invalidateQueries({ queryKey: ["videos"] });
  };

  const refresh = async () => {
    setBusy("refresh");
    const nv = await run(() => api.post<Video>(`/api/videos/${v.slug}/refresh`), { success: `${v.slug.toUpperCase()} 已刷新` });
    setBusy("");
    if (nv) update(nv);
  };
  const findSources = async () => {
    setBusy("probe");
    const nv = await run(() => api.post<Video & { probe: ProbeResult[] }>(`/api/videos/${v.slug}/probe`));
    setBusy("");
    if (nv) update({ ...nv, probe: nv.probe ?? [] });
  };

  const people = v.models.map(m => m.name).join("、");
  return (
    <>
      <header className="flex items-center gap-2 border-b border-line px-5 py-3">
        <Code className="text-xl">{v.slug}</Code>
        {v.status === "gone" && <Chip tone="err">已下架</Chip>}
        {!!v.uncensored && <Chip tone="info">无码流出</Chip>}
        <div className="ml-auto flex items-center gap-1.5">
          <Button size="sm" variant="primary" onClick={() => playVideo(v)}>
            <Play />
            试播
          </Button>
          <Button size="sm" onClick={refresh} disabled={!!busy} title="重新抓每个源的详情和播放地址">
            <RefreshCw className={busy === "refresh" ? "animate-spin" : ""} />
            {busy === "refresh" ? "刷新中…" : "刷新"}
          </Button>
          <Button size="sm" onClick={findSources} disabled={!!busy} title="到每个启用、还没有这部影片源的站点按番号找一次；要过 CF 的站最长约一分半">
            <Search />
            {busy === "probe" ? "查找中…" : "查找其他源"}
          </Button>
          <SheetClose className="ml-1 rounded p-1 text-muted hover:bg-panel-2 hover:text-ink" aria-label="关闭">
            <X className="size-4" />
          </SheetClose>
        </div>
      </header>

      <div className="scroll-thin min-h-0 flex-1 space-y-6 overflow-y-auto px-5 py-4">
        <div className="grid gap-4 sm:grid-cols-[240px_1fr]">
          {v.cover_url || v.thumb_url ? (
            <img referrerPolicy="no-referrer" src={v.cover_url || v.thumb_url} alt="" className="w-full rounded-md border border-line bg-panel-2 object-cover" />
          ) : (
            <div className="flex aspect-video items-center justify-center rounded-md border border-dashed border-line text-xs text-muted">没有封面</div>
          )}
          <div className="min-w-0">
            <h3 className="mb-3 font-medium leading-snug">{v.title}</h3>
            <KV
              items={[
                ["女优", people || "-"],
                ["上市", v.release_date || "-"],
                ["时长", fmtClockDur(v.duration) || "-"],
                ["番号", <Mono key="c">{v.code || "-"}</Mono>],
                ["发行商", [v.maker, v.director && `导演 ${v.director}`].filter(Boolean).join("，") || "-"],
                ["画质", v.quality || "-"],
                ["观看 / 收藏", `${fmtNum(v.views)} / ${fmtNum(v.favs)}`],
                ["入库", `${fmtTime(v.created_at)}${v.detail_at ? `，详情 ${fmtTime(v.detail_at)}` : "，没抓详情"}`],
              ]}
            />
          </div>
        </div>

        {(v.categories.length > 0 || v.tags.length > 0) && (
          <div className="flex flex-wrap gap-1">
            {v.categories.map(c => (
              <Chip key={"c" + (c.slug ?? c.name)}>{c.name}</Chip>
            ))}
            {v.tags.map(t => (
              <Chip key={"t" + (t.slug ?? t.name)} className="bg-transparent">
                #{t.name}
              </Chip>
            ))}
          </div>
        )}

        {probe && (
          <section className="rounded-md border border-line bg-panel-2 p-3">
            <h4 className="mb-2 text-[13px] font-medium">查找其他源的结果</h4>
            {probe.length === 0 ? (
              <p className="text-[13px] text-muted">所有启用的站点上都已经有这部影片的源了。</p>
            ) : (
              <ul className="space-y-1 text-[13px]">
                {probe.map(r => (
                  <li key={r.site} className="flex items-start gap-2">
                    <Chip tone={PROBE_STATUS[r.status]?.[1]}>{PROBE_STATUS[r.status]?.[0] ?? r.status}</Chip>
                    <span className="w-16 shrink-0">{r.label}</span>
                    <span className="min-w-0 break-all text-muted">{r.error || (r.found ? `加上 ${r.found} 个源` : "")}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        <section>
          <h4 className="mb-1 text-sm font-semibold">源</h4>
          <p className="mb-2 text-xs text-muted">按播放的优先顺序，第一个不能用时自动换下一个；多线路站点按设置里的线路顺序试。</p>
          {v.sources.length === 0 ? (
            <p className="text-[13px] text-muted">没有源。点「查找其他源」到各站按番号找。</p>
          ) : (
            <ul className="divide-y divide-line rounded-md border border-line">
              {v.sources.map(src => (
                <SourceRow key={src.id} v={v} src={src} />
              ))}
            </ul>
          )}
        </section>

        <section className="space-y-2">
          <h4 className="text-sm font-semibold">本服务播放地址</h4>
          <p className="text-xs text-muted">这就是 strm 文件里的内容。</p>
          <div className="flex items-center gap-2 rounded-md bg-panel-2 px-3 py-2">
            <Mono className="min-w-0 flex-1">{v.play_url}</Mono>
            <CopyButton text={v.play_url} size="icon-sm" />
          </div>
        </section>

        <section className="space-y-2">
          <h4 className="text-sm font-semibold">strm 文件</h4>
          {v.outputs.length === 0 ? (
            <p className="text-[13px] text-muted">不在任何输出库里。</p>
          ) : (
            v.outputs.map(o => (
              <div key={o.library_id} className="flex items-center gap-2 rounded-md bg-panel-2 px-3 py-2">
                <Chip tone="accent" className="shrink-0">
                  {o.library_name}
                </Chip>
                <Mono className="min-w-0 flex-1">{o.strm_path || "还没写出"}</Mono>
                {o.strm_path && <CopyButton text={o.strm_path} size="icon-sm" />}
              </div>
            ))
          )}
        </section>
      </div>
    </>
  );
}

function SourceRow({ v, src }: { v: Video; src: Source }) {
  const state = streamState(src);
  const lines = src.lines;
  return (
    <li className="px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{src.label}</span>
        {lines ? <Chip>{lines.length ? `${lines.length} 条线路` : "线路待取（播放或刷新时）"}</Chip> : <Chip>{src.direct ? "302" : "中转"}</Chip>}
        {src.subtitle && <Chip tone="info">{SUBTITLES[src.subtitle] ?? src.subtitle}</Chip>}
        {src.height ? <Chip>{src.height}p</Chip> : null}
        {!lines && <Chip tone={state.tone}>{state.text}</Chip>}
        {lines && src.status !== "active" && <Chip tone={state.tone}>{state.text}</Chip>}
        <div className="ml-auto flex items-center gap-1">
          {src.page_url && (
            <Button asChild size="sm" variant="ghost" title={src.page_url}>
              <a href={src.page_url} target="_blank" rel="noreferrer">
                <ExternalLink />
                {src.key}
              </a>
            </Button>
          )}
          {!lines && src.stream_url && <CopyButton text={src.stream_url} label="复制地址" size="icon-sm" />}
          {src.status === "active" && (
            <Button size="sm" onClick={() => playVideo(v, src)}>
              <Play />
              试播此源
            </Button>
          )}
        </div>
      </div>
      {src.last_error && <p className="mt-1 truncate text-xs text-err" title={src.last_error}>{src.last_error}</p>}
      {lines && lines.length > 0 && (
        <ul className="mt-2 space-y-1 border-l-2 border-line pl-3">
          {lines.map(ln => (
            <LineRow key={ln.id} v={v} src={src} ln={ln} />
          ))}
        </ul>
      )}
    </li>
  );
}

function LineRow({ v, src, ln }: { v: Video; src: Source; ln: SourceLine }) {
  const state = streamState(ln);
  const usable = src.status === "active" && ln.enabled && ln.supported;
  return (
    <li className={"flex flex-wrap items-center gap-2 text-[13px] " + (ln.enabled ? "" : "opacity-55")}>
      <span className="font-mono">{ln.line}</span>
      <span className="text-muted">{ln.host_label}</span>
      <Chip>{ln.direct ? (ln.ip_bound ? "302·绑 IP" : "302") : "中转"}</Chip>
      <Chip tone={ln.enabled ? state.tone : "neutral"}>{ln.enabled ? state.text : "已停用"}</Chip>
      {src.line === ln.line && <span className="text-xs text-accent">当前在用</span>}
      <div className="ml-auto flex items-center gap-1">
        {ln.stream_url && <CopyButton text={ln.stream_url} label="复制地址" size="icon-sm" />}
        {usable && (
          <Button size="sm" variant="ghost" onClick={() => playVideo(v, src, ln)}>
            <Play />
            试播
          </Button>
        )}
      </div>
      {ln.last_error && <p className="w-full truncate text-xs text-err" title={ln.last_error}>{ln.last_error}</p>}
    </li>
  );
}
