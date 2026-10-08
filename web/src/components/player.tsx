// 网页试播：经本服务中转（proxy=1），Safari 原生播 HLS，其他浏览器按需加载 hls.js

import { useEffect, useRef } from "react";
import { toast } from "sonner";
import type { Source, SourceLine, Video } from "@/lib/api";
import { createStore } from "@/lib/store";
import { Code } from "./ui/data";
import { Dialog, DialogContent } from "./ui/overlay";

interface PlayRequest {
  slug: string;
  title: string;
  src: string;
}

const store = createStore<PlayRequest | null>(null);

export function playVideo(v: Video, source?: Source, line?: SourceLine) {
  const token = new URL(v.play_url, location.href).searchParams.get("t");
  const q = new URLSearchParams({ proxy: "1" });
  if (source) q.set("src", source.site);
  if (line) q.set("line", line.line);
  if (token) q.set("t", token);
  const via = source ? `${source.label}${line ? " 线路 " + line.line : ""}` : "";
  store.set({ slug: v.slug, title: (via ? `[${via}] ` : "") + v.title, src: `/play/${v.slug}.m3u8?${q}` });
}

export function PlayerHost() {
  const req = store.use();
  return (
    <Dialog open={!!req} onOpenChange={open => !open && store.set(null)}>
      {req && (
        <DialogContent
          className="max-w-4xl"
          title={<Code className="text-lg">{req.slug}</Code>}
          description={<span className="line-clamp-1">{req.title}</span>}
        >
          <Player src={req.src} />
          <p className="mt-2 text-xs text-muted">网页试播经本服务中转；Emby / Jellyfin 按设置里的播放模式访问。</p>
        </DialogContent>
      )}
    </Dialog>
  );
}

function Player({ src }: { src: string }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = src;
      return;
    }
    let destroy = () => {};
    let cancelled = false;
    import("hls.js")
      .then(({ default: Hls }) => {
        if (cancelled) return;
        const hls = new Hls();
        hls.on(Hls.Events.ERROR, (_, d) => {
          if (d.fatal) toast.error(`播放失败：${d.details}`);
        });
        hls.loadSource(src);
        hls.attachMedia(video);
        destroy = () => hls.destroy();
      })
      .catch(e => toast.error(`播放器加载失败：${e}`));
    return () => {
      cancelled = true;
      destroy();
    };
  }, [src]);
  return <video ref={ref} controls autoPlay playsInline className="aspect-video w-full rounded-md bg-black" />;
}
