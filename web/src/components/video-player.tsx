// 网页试播：先识别响应类型，HLS 用 hls.js，MP4 用原生 video；控件和菜单随容器全屏。
// 快捷键由外层（对话框）把按键转给 handleKey，这样焦点在对话框里任何地方都能用。

import type Hls from "hls.js";
import {
  Check,
  FastForward,
  Gauge,
  LoaderCircle,
  Maximize,
  Minimize,
  Pause,
  PictureInPicture2,
  Play,
  Rewind,
  RotateCcw,
  Volume1,
  Volume2,
  VolumeX,
} from "lucide-react";
import {
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  type Ref,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { cn } from "@/lib/utils";
import { sourceLabel, usePlaybackSource } from "./use-playback-source";
import { Button } from "./ui/button";

export interface PlayerHandle {
  focus(): void;
  /** 处理一次按键；认得的快捷键会 preventDefault。 */
  handleKey(e: KeyboardEvent): void;
  /** 按了 Esc：先关菜单、退出全屏；处理了返回 true（对话框就不关）。 */
  handleEscape(): boolean;
}

interface Level {
  index: number;
  height: number;
  bitrate: number;
}

type Menu = "speed" | "quality" | null;

const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];
const PREFS_KEY = "hls2strm.player";
const SEEK_STEP = 5;
const SEEK_BIG = 60;
const IDLE_MS = 2500;

// 音量、静音、倍速按浏览器记住；读写失败（隐私模式等）就用默认值
function loadPrefs(): { volume: number; muted: boolean; rate: number } {
  const d = { volume: 1, muted: false, rate: 1 };
  try {
    const p = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    return {
      volume: typeof p.volume === "number" ? clamp(p.volume, 0, 1) : d.volume,
      muted: typeof p.muted === "boolean" ? p.muted : d.muted,
      rate: RATES.includes(p.rate) ? p.rate : d.rate,
    };
  } catch {
    return d;
  }
}

function savePrefs(p: { volume: number; muted: boolean; rate: number }) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(p));
  } catch {
    /* 存不了就算了 */
  }
}

const clamp = (x: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, x));

/** 秒 → 1:23 / 1:02:03；long 时分钟补到两位并带小时（总长过 1 小时，让两边宽度一致）。 */
function fmt(s: number, long = false): string {
  const t = Math.max(0, Math.floor(Number.isFinite(s) ? s : 0));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = String(t % 60).padStart(2, "0");
  return h || long ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

const levelLabel = (l: Level) => (l.height ? `${l.height}p` : `${Math.round(l.bitrate / 1000)} kbps`);

/** hls.js 出错时的说明：清单请求失败带上本服务返回的原因（{"detail": "…"}）。 */
function hlsErrorText(d: { details: string; response?: { code?: number }; networkDetails?: unknown }): string {
  const xhr = d.networkDetails as XMLHttpRequest | undefined;
  let detail = "";
  try {
    detail = JSON.parse(xhr?.responseText || "").detail || "";
  } catch {
    /* 不是 JSON */
  }
  const code = d.response?.code ? `HTTP ${d.response.code}` : "";
  return detail || [code, d.details].filter(Boolean).join(" · ");
}

export function VideoPlayer({ src, ref }: { src: string; ref?: Ref<PlayerHandle> }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const source = usePlaybackSource(videoRef, src);
  const resumeAt = source.resumeAt;
  const chosenHeight = useRef<number | null>(null); // null 默认最高；-1 自动；其余为手动档位。
  const [prefs] = useState(loadPrefs);

  const [playing, setPlaying] = useState(false);
  const [waiting, setWaiting] = useState(true);
  const error = source.error;
  const failRef = useRef(source.fail);
  failRef.current = source.fail;
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [buffered, setBuffered] = useState<[number, number][]>([]);
  const [volume, setVolume] = useState(prefs.volume);
  const [muted, setMuted] = useState(prefs.muted);
  const [rate, setRate] = useState(prefs.rate);
  const [levels, setLevels] = useState<Level[]>([]);
  const [level, setLevel] = useState(-1); // 选的档位，-1 自动
  const [height, setHeight] = useState(0); // 正在播的画面高度
  const [fullscreen, setFullscreen] = useState(false);
  const [menu, setMenu] = useState<Menu>(null);
  const [active, setActive] = useState(true); // 鼠标最近动过：显示控件
  const [overControls, setOverControls] = useState(false);
  const [osd, setOsd] = useState<{ node: ReactNode; key: number } | null>(null);
  const idleTimer = useRef(0);
  const osdTimer = useRef(0);

  // ---- 加载 ----

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    setWaiting(true);
    setLevels([]);
    setLevel(-1);
    let cancelled = false;
    let hls: Hls | null = null;
    const load = async () => {
      if (!source.prepared) return;
      const mediaSrc = source.prepared.url;
      if (source.prepared.media_type !== "hls") {
        video.src = mediaSrc;
        video.load();
        return;
      }
      const { default: HlsClass } = await import("hls.js");
      if (cancelled) return;
      if (!HlsClass.isSupported()) {
        if (!video.canPlayType("application/vnd.apple.mpegurl")) throw new Error("这个浏览器播不了 HLS");
        const nativeSrc = new URL(mediaSrc);
        nativeSrc.searchParams.set("variants", "highest");
        video.src = nativeSrc.href;
        return;
      }
      hls = new HlsClass({ autoStartLoad: false });
      hlsRef.current = hls;
      let networkRetried = false;
      let mediaRecovered = false;
      hls.on(HlsClass.Events.MANIFEST_PARSED, () => {
        if (!hls) return;
        const available = hls.levels.map((l, index) => ({ index, height: l.height, bitrate: l.bitrate }));
        setLevels(available);
        const ordered = [...available].sort((a, b) => b.height - a.height || b.bitrate - a.bitrate);
        const preferred = chosenHeight.current;
        const best = preferred && preferred > 0 ? ordered.find(l => l.height === preferred) || ordered[0] : ordered[0];
        if (best) {
          hls.startLevel = best.index;
          hls.loadLevel = preferred === -1 ? -1 : best.index;
          setLevel(preferred === -1 ? -1 : best.index);
        }
        hls.startLoad(resumeAt.current || -1);
      });
      hls.on(HlsClass.Events.LEVEL_SWITCHED, (_, d) => setHeight(hls?.levels[d.level]?.height || 0));
      hls.on(HlsClass.Events.ERROR, (_, d) => {
        if (!d.fatal || !hls) return;
        // 清单本身取不到（本服务找源失败、源站拦截）重试也没用，直接显示原因；分片断了再续一次
        const network = d.type === HlsClass.ErrorTypes.NETWORK_ERROR && !d.details.startsWith("manifest");
        if (network && !networkRetried) {
          networkRetried = true;
          hls.startLoad();
        } else if (d.type === HlsClass.ErrorTypes.MEDIA_ERROR && !mediaRecovered) {
          mediaRecovered = true;
          hls.recoverMediaError();
        } else {
          failRef.current(hlsErrorText(d));
          setWaiting(false);
        }
      });
      hls.loadSource(mediaSrc);
      hls.attachMedia(video);
    };
    void load().catch(e => {
      if (cancelled) return;
      failRef.current(e instanceof Error ? e.message : `播放器加载失败：${e}`);
      setWaiting(false);
    });
    return () => {
      cancelled = true;
      hls?.destroy();
      hlsRef.current = null;
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
  }, [source.prepared]);

  // 偏好应用到 video 上（换源会重置倍速，所以 defaultPlaybackRate 也设）
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    v.volume = prefs.volume;
    v.muted = prefs.muted;
    v.defaultPlaybackRate = v.playbackRate = prefs.rate;
  }, [prefs]);

  useEffect(() => savePrefs({ volume, muted, rate }), [volume, muted, rate]);

  useEffect(() => {
    const onFs = () => setFullscreen(document.fullscreenElement === rootRef.current);
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  useEffect(() => {
    const v = videoRef.current;
    const onResize = () => v && setHeight(v.videoHeight);
    v?.addEventListener("resize", onResize);
    return () => v?.removeEventListener("resize", onResize);
  }, []);

  useEffect(
    () => () => {
      clearTimeout(idleTimer.current);
      clearTimeout(osdTimer.current);
    },
    [],
  );

  // ---- 操作 ----

  const flash = useCallback((node: ReactNode) => {
    clearTimeout(osdTimer.current);
    setOsd({ node, key: Date.now() });
    osdTimer.current = window.setTimeout(() => setOsd(null), 800);
  }, []);

  const poke = useCallback(() => {
    setActive(true);
    clearTimeout(idleTimer.current);
    idleTimer.current = window.setTimeout(() => setActive(false), IDLE_MS);
  }, []);

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v || error) return;
    if (v.paused || v.ended) v.play().catch(() => {});
    else v.pause();
  }, [error]);

  const seekTo = useCallback((t: number) => {
    const v = videoRef.current;
    if (!v || !Number.isFinite(v.duration)) return;
    v.currentTime = clamp(t, 0, Math.max(0, v.duration - 0.5));
    setTime(v.currentTime);
  }, []);

  const seekBy = useCallback(
    (d: number) => {
      const v = videoRef.current;
      if (!v) return;
      seekTo(v.currentTime + d);
      flash(
        <>
          {d < 0 ? <Rewind /> : <FastForward />}
          {d < 0 ? "−" : "+"}
          {Math.abs(d) >= 60 ? `${Math.abs(d) / 60} 分钟` : `${Math.abs(d)} 秒`}
        </>,
      );
    },
    [seekTo, flash],
  );

  const setVol = useCallback(
    (x: number, show = true) => {
      const v = videoRef.current;
      if (!v) return;
      const nv = Math.round(clamp(x, 0, 1) * 100) / 100;
      v.volume = nv;
      v.muted = nv === 0;
      if (show) flash(nv === 0 ? <><VolumeX />静音</> : <><Volume2 />音量 {Math.round(nv * 100)}%</>);
    },
    [flash],
  );

  const toggleMute = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.muted || v.volume === 0) {
      v.muted = false;
      if (v.volume === 0) v.volume = 0.5;
      flash(<><Volume2 />音量 {Math.round(v.volume * 100)}%</>);
    } else {
      v.muted = true;
      flash(<><VolumeX />静音</>);
    }
  }, [flash]);

  const changeRate = useCallback(
    (r: number) => {
      const v = videoRef.current;
      if (!v) return;
      v.defaultPlaybackRate = v.playbackRate = r;
      flash(<><Gauge />{r}×</>);
    },
    [flash],
  );

  const stepRate = useCallback(
    (dir: 1 | -1) => {
      const i = RATES.indexOf(videoRef.current?.playbackRate ?? 1);
      changeRate(RATES[clamp((i < 0 ? RATES.indexOf(1) : i) + dir, 0, RATES.length - 1)]);
    },
    [changeRate],
  );

  const pickLevel = useCallback((index: number) => {
    const hls = hlsRef.current;
    if (!hls) return;
    hls.currentLevel = index; // -1 回到自动
    chosenHeight.current = index === -1 ? -1 : hls.levels[index]?.height || null;
    setLevel(index);
    setMenu(null);
  }, []);

  const toggleFullscreen = useCallback(() => {
    const root = rootRef.current;
    const v = videoRef.current as (HTMLVideoElement & { webkitEnterFullscreen?: () => void }) | null;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else if (root?.requestFullscreen) root.requestFullscreen().catch(() => {});
    else v?.webkitEnterFullscreen?.(); // iOS Safari 只能让 video 自己全屏
  }, []);

  const togglePip = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {});
    else v.requestPictureInPicture().catch(() => {});
  }, []);

  const retry = () => source.change("重新尝试", false, true);

  const handleKey = useCallback(
    (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
      const target = e.target as HTMLElement;
      if (target.closest("input, textarea, select, [contenteditable='true']")) return;
      // 用 Tab 聚焦到按钮上时，空格、回车交给按钮自己
      if ((e.key === " " || e.key === "Enter") && target.closest("button")) return;
      const v = videoRef.current;
      if (!v) return;
      const big = e.shiftKey ? SEEK_BIG : SEEK_STEP;
      // 倍速键按物理键认：有的输入方式 Shift+, 报的是 ","（带 shiftKey），不是 "<"
      const key = e.shiftKey && e.code === "Comma" ? "<" : e.shiftKey && e.code === "Period" ? ">" : e.key;
      if (/^[0-9]$/.test(key)) {
        seekTo((v.duration || 0) * (Number(key) / 10));
      } else {
        switch (key) {
          case " ":
          case "k":
          case "K":
            togglePlay();
            break;
          case "ArrowLeft":
            seekBy(-big);
            break;
          case "ArrowRight":
            seekBy(big);
            break;
          case "ArrowUp":
            setVol((v.muted ? 0 : v.volume) + 0.1);
            break;
          case "ArrowDown":
            setVol((v.muted ? 0 : v.volume) - 0.1);
            break;
          case "m":
          case "M":
            toggleMute();
            break;
          case "f":
          case "F":
            toggleFullscreen();
            break;
          case "<":
            stepRate(-1);
            break;
          case ">":
            stepRate(1);
            break;
          case "Home":
            seekTo(0);
            break;
          case "End":
            seekTo(v.duration || 0);
            break;
          default:
            return;
        }
      }
      e.preventDefault();
      poke();
    },
    [seekTo, seekBy, setVol, togglePlay, toggleMute, toggleFullscreen, stepRate, poke],
  );

  useImperativeHandle(
    ref,
    () => ({
      focus: () => rootRef.current?.focus(),
      handleKey,
      handleEscape: () => {
        if (menu) {
          setMenu(null);
          return true;
        }
        if (document.fullscreenElement) {
          document.exitFullscreen().catch(() => {});
          return true;
        }
        return false;
      },
    }),
    [handleKey, menu],
  );

  // ---- 界面 ----

  const long = duration >= 3600;
  const hidden = !active && playing && !menu && !overControls;
  const shownHeight = height || levels.find(l => l.index === level)?.height || 0;
  const pipOk = typeof document !== "undefined" && document.pictureInPictureEnabled;

  return (
    <>
    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs" aria-live="polite">
      <span className="min-w-0 flex-1 break-words text-muted">
        {source.message || (source.current ? `当前源：${sourceLabel(source.current)}` : "尚未起播")}
      </span>
      <label className="flex items-center gap-1"><input type="checkbox" checked={source.automatic}
        onChange={e => source.setAutomatic(e.target.checked)} />自动换源</label>
      <Button size="sm" disabled={source.preparing} onClick={() => source.change("手动换源")}>换一个源</Button>
      {source.pinned && <Button size="sm" disabled={source.preparing} onClick={source.useAllSources}>使用全部源</Button>}
    </div>
    <div
      ref={rootRef}
      tabIndex={-1}
      onPointerMove={poke}
      onPointerLeave={() => setActive(false)}
      className={cn(
        "relative aspect-video w-full select-none overflow-hidden rounded-md bg-black text-white outline-none",
        fullscreen && "aspect-auto h-full rounded-none",
        hidden && "cursor-none",
      )}
    >
      <video
        ref={videoRef}
        autoPlay
        playsInline
        className="size-full"
        onClick={() => {
          if (menu) setMenu(null);
          else togglePlay();
        }}
        onDoubleClick={toggleFullscreen}
        onPlay={() => { setPlaying(true); source.pauseState(false); }}
        onPause={() => { setPlaying(false); source.pauseState(true); }}
        onEnded={() => setPlaying(false)}
        onWaiting={() => setWaiting(true)}
        onPlaying={() => setWaiting(false)}
        onCanPlay={() => setWaiting(false)}
        onSeeking={() => { setWaiting(true); source.seeking(); }}
        onSeeked={() => setWaiting(false)}
        onLoadedMetadata={e => {
          const el = e.currentTarget;
          setDuration(el.duration);
          setHeight(el.videoHeight);
          source.restore(el);
        }}
        onDurationChange={e => setDuration(e.currentTarget.duration)}
        onTimeUpdate={e => { setTime(e.currentTarget.currentTime); source.progressed(); }}
        onProgress={e => {
          const el = e.currentTarget;
          const d = el.duration;
          if (!Number.isFinite(d) || d <= 0) return;
          const out: [number, number][] = [];
          for (let i = 0; i < el.buffered.length; i++) out.push([el.buffered.start(i) / d, el.buffered.end(i) / d]);
          setBuffered(out);
        }}
        onVolumeChange={e => {
          setVolume(e.currentTarget.volume);
          setMuted(e.currentTarget.muted);
        }}
        onRateChange={e => setRate(e.currentTarget.playbackRate)}
        onError={e => {
          if (hlsRef.current) return; // hls.js 自己报
          source.fail(`播放失败（${e.currentTarget.error?.message || `错误码 ${e.currentTarget.error?.code ?? "?"}`}）`);
          setWaiting(false);
        }}
      />

      {fullscreen && <div className="absolute inset-x-0 top-0 z-20 flex items-center gap-3 bg-black/60 p-3 text-xs">
        <span className="flex-1">{source.message || (source.current ? `当前源：${sourceLabel(source.current)}` : "正在选源")}</span>
        <button type="button" disabled={source.preparing} onClick={() => source.change("手动换源")}>换一个源</button>
      </div>}

      {waiting && !error && (
        <div className="pointer-events-none absolute inset-0 grid place-items-center">
          <LoaderCircle className="size-10 animate-spin text-white/80" />
        </div>
      )}

      {!playing && !waiting && !error && (
        <button
          type="button"
          onClick={togglePlay}
          onMouseDown={e => e.preventDefault()}
          aria-label="播放"
          className="absolute left-1/2 top-1/2 grid size-16 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full bg-black/55 text-white transition-colors hover:bg-accent"
        >
          <Play className="ml-1 size-7 fill-current" />
        </button>
      )}

      {osd && (
        <div
          key={osd.key}
          className="anim-fade pointer-events-none absolute left-1/2 top-[16%] flex -translate-x-1/2 items-center gap-2 rounded-md bg-black/70 px-3 py-1.5 text-sm font-medium [&_svg]:size-4"
        >
          {osd.node}
        </div>
      )}

      {error && (
        <div className="absolute inset-0 grid place-items-center bg-black/75 p-6 text-center">
          <div className="max-w-md">
            <p className="text-sm font-medium">播放失败</p>
            <p className="mt-1.5 break-all text-[13px] text-white/70">{error}</p>
            <button
              type="button"
              onClick={retry}
              className="mt-4 inline-flex items-center gap-1.5 rounded-md bg-white/15 px-3 py-1.5 text-sm hover:bg-white/25 [&_svg]:size-4"
            >
              <RotateCcw />
              重试
            </button>
          </div>
        </div>
      )}

      {menu && (
        <div
          className="anim-fade absolute bottom-14 right-3 z-10 min-w-32 rounded-lg bg-black/85 p-1 text-sm shadow-lg backdrop-blur"
          onPointerMove={e => e.stopPropagation()}
        >
          <p className="px-2.5 pb-1 pt-1.5 text-xs text-white/55">{menu === "speed" ? "播放速度" : "画质"}</p>
          {menu === "speed"
            ? RATES.map(r => (
                <MenuItem key={r} on={r === rate} onClick={() => (changeRate(r), setMenu(null))}>
                  {r === 1 ? "正常" : `${r}×`}
                </MenuItem>
              ))
            : [
                <MenuItem key="auto" on={level === -1} onClick={() => pickLevel(-1)}>
                  自动{level === -1 && height ? <span className="text-white/55">（{height}p）</span> : null}
                </MenuItem>,
                ...[...levels]
                  .sort((a, b) => b.height - a.height || b.bitrate - a.bitrate)
                  .map(l => (
                    <MenuItem key={l.index} on={level === l.index} onClick={() => pickLevel(l.index)}>
                      {levelLabel(l)}
                    </MenuItem>
                  )),
              ]}
        </div>
      )}

      <div
        onPointerEnter={() => setOverControls(true)}
        onPointerLeave={() => setOverControls(false)}
        className={cn(
          "absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 via-black/45 to-transparent px-3 pb-1.5 pt-12 transition-opacity duration-200",
          hidden && "pointer-events-none opacity-0",
        )}
      >
        <Slider
          label="播放进度"
          value={duration ? time / duration : 0}
          ranges={buffered}
          valueText={`${fmt(time, long)} / ${fmt(duration, long)}`}
          tip={f => fmt(f * duration, long)}
          onCommit={f => seekTo(f * duration)}
          className="h-4"
        />
        <div className="flex items-center gap-0.5">
          <Ctl label={playing ? "暂停（空格）" : "播放（空格）"} onClick={togglePlay}>
            {playing ? <Pause className="fill-current" /> : <Play className="fill-current" />}
          </Ctl>
          <Ctl label={`后退 ${SEEK_STEP} 秒（←）`} onClick={() => seekBy(-SEEK_STEP)} className="max-sm:hidden">
            <Rewind />
          </Ctl>
          <Ctl label={`前进 ${SEEK_STEP} 秒（→）`} onClick={() => seekBy(SEEK_STEP)} className="max-sm:hidden">
            <FastForward />
          </Ctl>
          <div className="group/vol flex items-center">
            <Ctl label={muted ? "取消静音（M）" : "静音（M）"} onClick={toggleMute}>
              {muted || volume === 0 ? <VolumeX /> : volume < 0.5 ? <Volume1 /> : <Volume2 />}
            </Ctl>
            <Slider
              label="音量"
              value={muted ? 0 : volume}
              valueText={`${Math.round((muted ? 0 : volume) * 100)}%`}
              onChange={f => setVol(f, false)}
              onCommit={f => setVol(f, false)}
              className="h-6 w-0 opacity-0 transition-all group-hover/vol:mr-1 group-hover/vol:w-20 group-hover/vol:opacity-100 max-sm:hidden"
            />
          </div>
          <span className="ml-1.5 whitespace-nowrap font-mono text-xs text-white/85">
            {fmt(time, long)}
            <span className="text-white/45"> / {fmt(duration, long)}</span>
          </span>
          <div className="flex-1" />
          <Ctl label="播放速度（< >）" onClick={() => setMenu(m => (m === "speed" ? null : "speed"))} wide on={menu === "speed"}>
            <span className={cn("text-xs font-semibold", rate !== 1 && "text-accent")}>{rate}×</span>
          </Ctl>
          {levels.length > 1 ? (
            <Ctl label="画质" onClick={() => setMenu(m => (m === "quality" ? null : "quality"))} wide on={menu === "quality"}>
              <span className="text-xs font-semibold">
                {shownHeight ? `${shownHeight}p` : "画质"}
                {level === -1 && <span className="font-normal text-white/55"> · 自动</span>}
              </span>
            </Ctl>
          ) : (
            shownHeight > 0 && (
              <span className="px-1.5 text-xs font-semibold text-white/85" title="画面分辨率">
                {shownHeight}p
              </span>
            )
          )}
          {pipOk && (
            <Ctl label="画中画" onClick={togglePip} className="max-sm:hidden">
              <PictureInPicture2 />
            </Ctl>
          )}
          <Ctl label={fullscreen ? "退出全屏（F）" : "全屏（F）"} onClick={toggleFullscreen}>
            {fullscreen ? <Minimize /> : <Maximize />}
          </Ctl>
        </div>
      </div>
    </div>
    </>
  );
}

/** 控件按钮：按下时不抢焦点，快捷键一直有效；用 Tab 聚焦时照常能按。 */
function Ctl({
  label,
  onClick,
  children,
  className,
  wide,
  on,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
  className?: string;
  wide?: boolean;
  on?: boolean;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      onMouseDown={e => e.preventDefault()}
      className={cn(
        "grid h-8 shrink-0 place-items-center rounded-md text-white/90 transition-colors hover:bg-white/15 hover:text-white [&_svg]:size-[18px]",
        wide ? "min-w-10 px-2" : "w-8",
        on && "bg-white/15",
        className,
      )}
    >
      {children}
    </button>
  );
}

function MenuItem({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseDown={e => e.preventDefault()}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left hover:bg-white/15",
        on ? "text-white" : "text-white/80",
      )}
    >
      <Check className={cn("size-3.5 shrink-0 text-accent", !on && "invisible")} />
      <span className="flex-1">{children}</span>
    </button>
  );
}

/** 横向滑条（进度、音量）：点、拖都行；拖动时只改显示，松手才 onCommit（进度条拖动中不反复 seek）。 */
function Slider({
  label,
  value,
  valueText,
  ranges,
  tip,
  onChange,
  onCommit,
  className,
}: {
  label: string;
  value: number;
  valueText: string;
  ranges?: [number, number][];
  tip?: (f: number) => string;
  onChange?: (f: number) => void;
  onCommit: (f: number) => void;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<number | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const at = (e: PointerEvent) => {
    const r = ref.current!.getBoundingClientRect();
    return r.width ? clamp((e.clientX - r.left) / r.width, 0, 1) : 0;
  };
  const shown = drag ?? clamp(value, 0, 1);
  const tipAt = drag ?? hover;
  return (
    <div
      ref={ref}
      role="slider"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(shown * 100)}
      aria-valuetext={valueText}
      className={cn("group/bar relative flex cursor-pointer touch-none items-center", className)}
      onPointerDown={e => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        const f = at(e);
        setDrag(f);
        onChange?.(f);
      }}
      onPointerMove={e => {
        const f = at(e);
        setHover(f);
        if (drag !== null) {
          setDrag(f);
          onChange?.(f);
        }
      }}
      onPointerUp={e => {
        if (drag === null) return;
        onCommit(at(e));
        setDrag(null);
      }}
      onPointerCancel={() => setDrag(null)}
      onPointerLeave={() => setHover(null)}
    >
      <div className="relative h-1 w-full overflow-hidden rounded-full bg-white/25 transition-[height] group-hover/bar:h-1.5">
        {ranges?.map(([a, b], i) => (
          <div key={i} className="absolute inset-y-0 bg-white/35" style={{ left: `${a * 100}%`, width: `${(b - a) * 100}%` }} />
        ))}
        <div className="absolute inset-y-0 left-0 bg-accent" style={{ width: `${shown * 100}%` }} />
      </div>
      <div
        className={cn(
          "pointer-events-none absolute size-3 -translate-x-1/2 rounded-full bg-accent shadow transition-transform",
          drag === null && "scale-0 group-hover/bar:scale-100",
        )}
        style={{ left: `${shown * 100}%` }}
      />
      {tip && tipAt !== null && (
        <div
          className="pointer-events-none absolute bottom-full mb-1.5 -translate-x-1/2 rounded bg-black/85 px-1.5 py-0.5 font-mono text-[11px] text-white"
          style={{ left: `clamp(28px, ${tipAt * 100}%, calc(100% - 28px))` }}
        >
          {tip(tipAt)}
        </div>
      )}
    </div>
  );
}
