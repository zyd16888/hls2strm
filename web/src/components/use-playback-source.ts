import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

export interface PlaybackSource {
  source_id: number;
  line_id: number;
  label: string;
  line: string;
  host: string;
  media_type: "hls" | "file";
  height: number;
  duration: number;
  url: string;
}

const key = (source: PlaybackSource) => `${source.source_id}:${source.line_id}`;
export const sourceLabel = (source: PlaybackSource) =>
  `${source.label}${source.line ? ` · ${source.line} / ${source.host}` : ""} · ${source.media_type === "hls" ? "HLS" : "MP4"} · 中转`;

/** 一次恢复有总预算和次数限制，换源始终建立新会话，旧异步结果不能覆盖新请求。 */
export function usePlaybackSource(video: RefObject<HTMLVideoElement | null>, src: string) {
  const pinned = !!new URL(src, location.href).searchParams.get("src");
  const [automatic, setAutomatic] = useState(!pinned);
  const [allSources, setAllSources] = useState(false);
  const [version, setVersion] = useState(0);
  const [prepared, setPrepared] = useState<PlaybackSource | null>(null);
  const [current, setCurrent] = useState<PlaybackSource | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("正在选源…");
  const [preparing, setPreparing] = useState(true);
  const tried = useRef(new Set<string>());
  const snapshot = useRef({ time: 0, duration: 0, paused: false, volume: 1, muted: false, rate: 1 });
  const resumeAt = useRef(0);
  const recovering = useRef(false);
  const recovery = useRef({ count: 0, until: 0 });
  const progress = useRef({ time: 0, at: performance.now(), grace: performance.now() + 10000 });
  const switchReason = useRef("");
  const stable = useRef({ key: "", since: 0 });
  const userPaused = useRef(false);

  const change = useCallback((reason: string, auto = false, retry = false) => {
    if (preparing) return false;
    if (auto && (!automatic || recovery.current.count >= 3 ||
      (recovery.current.until > 0 && performance.now() >= recovery.current.until))) return false;
    const v = video.current;
    // 连续候选失败时沿用最初进度，不能用尚未起播的新 video 的 0 覆盖。
    if (v && !recovering.current) {
      snapshot.current = { time: v.currentTime, duration: v.duration, paused: v.paused && v.readyState >= 2,
        volume: v.volume, muted: v.muted, rate: v.playbackRate };
      resumeAt.current = v.currentTime;
    }
    userPaused.current = snapshot.current.paused;
    if (!auto) recovery.current = { count: 0, until: performance.now() + 30000 };
    if (!recovery.current.until) recovery.current.until = performance.now() + 30000;
    if (auto) recovery.current.count++;
    if (!retry && prepared) tried.current.add(key(prepared));
    if (retry) tried.current.clear();
    switchReason.current = reason;
    recovering.current = true;
    setError("");
    setMessage(`${reason}，正在选择${retry ? "" : "其他"}源…`);
    setPreparing(true);
    setPrepared(null);
    setVersion(value => value + 1);
    return true;
  }, [automatic, prepared, preparing, video]);

  const fail = useCallback((reason: string) => {
    if (!change(reason, true)) {
      setError(reason + (automatic ? "；自动恢复已停止，可手动重试或换源" : ""));
      setMessage("");
    }
  }, [automatic, change]);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    const target = new URL(src, location.href);
    target.searchParams.set("prepare", "1");
    target.searchParams.set("proxy", "1");
    target.searchParams.set("skip", [...tried.current].join(","));
    if (allSources) { target.searchParams.delete("src"); target.searchParams.delete("line"); }
    const load = async () => {
      const response = await fetch(target, { signal: controller.signal, cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `选源失败（HTTP ${response.status}）`);
      if (cancelled) return;
      const source = { ...body, url: new URL(body.url, response.url).href } as PlaybackSource;
      setPrepared(source);
      setPreparing(false);
      setMessage(`${switchReason.current ? switchReason.current + "；" : ""}正在加载：${sourceLabel(source)}`);
      progress.current = { time: 0, at: performance.now(), grace: performance.now() + 10000 };
    };
    void load().catch(e => {
      if (cancelled) return;
      setPreparing(false);
      setError(e instanceof Error ? e.message : String(e));
      setMessage("");
    });
    const remaining = recovering.current ? Math.max(1, recovery.current.until - performance.now()) : 120000;
    const timer = window.setTimeout(() => controller.abort(), remaining);
    return () => { cancelled = true; controller.abort(); clearTimeout(timer); };
  }, [src, version, allSources]);

  useEffect(() => {
    if (!prepared || !automatic || error || preparing) return;
    const timer = window.setInterval(() => {
      const v = video.current;
      if (!v) return;
      const now = performance.now();
      if (document.hidden || v.ended || v.seeking || userPaused.current || (v.paused && v.readyState >= 2)) {
        progress.current.at = now;
        progress.current.grace = now + 5000;
        return;
      }
      if (v.currentTime > progress.current.time + 0.05) {
        progress.current = { ...progress.current, time: v.currentTime, at: now };
        return;
      }
      let buffer = 0;
      for (let i = 0; i < v.buffered.length; i++) {
        if (v.buffered.start(i) <= v.currentTime && v.buffered.end(i) >= v.currentTime)
          buffer = v.buffered.end(i) - v.currentTime;
      }
      if (buffer < 2 && now > progress.current.grace && now - progress.current.at >= 8000)
        fail("当前源持续卡顿");
    }, 1000);
    return () => clearInterval(timer);
  }, [prepared, automatic, error, preparing, video, fail]);

  const restore = (v: HTMLVideoElement) => {
    if (!recovering.current) return true;
    const saved = snapshot.current;
    if (saved.time > 0 && Number.isFinite(saved.duration) && Number.isFinite(v.duration) &&
      Math.abs(v.duration - saved.duration) > Math.max(10, saved.duration * 0.01)) {
      v.pause();
      setError("新源时长与原源不一致，已停止自动续播；请重新选择影片源核对版本");
      setMessage("");
      return false;
    }
    v.volume = saved.volume;
    v.muted = saved.muted;
    v.defaultPlaybackRate = v.playbackRate = saved.rate;
    if (saved.time && Number.isFinite(v.duration)) v.currentTime = Math.min(saved.time, Math.max(0, v.duration - 0.5));
    if (saved.paused) v.pause();
    if (saved.paused) setMessage(`新源已就绪（保持暂停）：${prepared ? sourceLabel(prepared) : ""}`);
    return true;
  };
  const progressed = () => {
    if (!prepared || !video.current || video.current.currentTime <= 0 || error || video.current.paused ||
      (recovering.current && video.current.currentTime <= snapshot.current.time + 0.05)) return;
    setCurrent(prepared);
    setMessage("");
    recovering.current = false;
    resumeAt.current = 0;
    const id = key(prepared);
    if (stable.current.key !== id) stable.current = { key: id, since: performance.now() };
    if (performance.now() - stable.current.since > 30000) recovery.current = { count: 0, until: 0 };
    // 稳定播放 30 秒后才恢复预算，避免反复“成功一帧→卡顿”无限切换。
  };
  const useAllSources = () => { tried.current.clear(); setAllSources(true); setAutomatic(true); change("已切回全部源", false, true); };
  const pauseState = (paused: boolean) => { if (!preparing && !recovering.current) userPaused.current = paused; };
  const seeking = () => { progress.current = { time: video.current?.currentTime || 0, at: performance.now(), grace: performance.now() + 5000 }; };
  return { prepared, current, error, message, preparing, automatic, setAutomatic, resumeAt,
    fail, change, restore, progressed, pauseState, seeking, pinned: pinned && !allSources, useAllSources };
}
