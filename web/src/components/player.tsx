// 网页试播：经本服务中转（proxy=1），多码率的源把各档都要来（variants=all），播放器里能切画质

import { useRef } from "react";
import type { Source, SourceLine, Video } from "@/lib/api";
import { createStore } from "@/lib/store";
import { Code } from "./ui/data";
import { Dialog, DialogContent } from "./ui/overlay";
import { type PlayerHandle, VideoPlayer } from "./video-player";

interface PlayRequest {
  slug: string;
  title: string;
  src: string;
}

const store = createStore<PlayRequest | null>(null);

const SHORTCUTS: [string, string][] = [
  ["空格", "播放 / 暂停"],
  ["← →", "5 秒"],
  ["Shift + ← →", "1 分钟"],
  ["↑ ↓", "音量"],
  ["M", "静音"],
  ["F", "全屏"],
  ["0–9", "跳到 0%–90%"],
  ["< >", "倍速"],
];

export function playVideo(v: Video, source?: Source, line?: SourceLine) {
  const token = new URL(v.play_url, location.href).searchParams.get("t");
  const q = new URLSearchParams({ proxy: "1", variants: "all" });
  if (source) q.set("src", source.site);
  if (line) q.set("line", line.line);
  if (token) q.set("t", token);
  const via = source ? `${source.label}${line ? " 线路 " + line.line : ""}` : "";
  store.set({ slug: v.slug, title: (via ? `[${via}] ` : "") + v.title, src: `/play/${v.slug}.m3u8?${q}` });
}

export function PlayerHost() {
  const req = store.use();
  const player = useRef<PlayerHandle>(null);
  return (
    <Dialog open={!!req} onOpenChange={open => !open && store.set(null)}>
      {req && (
        <DialogContent
          className="max-w-5xl"
          title={<Code className="text-lg">{req.slug}</Code>}
          description={<span className="line-clamp-1">{req.title}</span>}
          onOpenAutoFocus={e => {
            e.preventDefault();
            player.current?.focus();
          }}
          onKeyDown={e => player.current?.handleKey(e)}
          onEscapeKeyDown={e => {
            if (player.current?.handleEscape()) e.preventDefault();
          }}
        >
          <VideoPlayer key={req.src} ref={player} src={req.src} />
          <div className="mt-2.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted max-sm:hidden">
            {SHORTCUTS.map(([k, what]) => (
              <span key={k} className="whitespace-nowrap">
                <kbd className="rounded border border-line bg-panel-2 px-1 py-px font-mono text-[11px] text-ink">{k}</kbd> {what}
              </span>
            ))}
          </div>
          <p className="mt-1.5 text-xs text-muted">网页试播经本服务中转；Emby / Jellyfin 按设置里的播放模式访问。</p>
        </DialogContent>
      )}
    </Dialog>
  );
}
