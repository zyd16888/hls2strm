import { FileVideo, Film, FolderTree, LayoutDashboard, ListChecks, LogOut, Menu as MenuIcon, Monitor, Moon, Pause, Play, RotateCw, ScrollText, Settings2, Sun } from "lucide-react";
import { type ComponentType, lazy, Suspense, useEffect, useState } from "react";
import { ConfirmHost } from "./components/confirm";
import { PlayerHost } from "./components/player";
import { SiteSignals } from "./components/site-signals";
import { Button } from "./components/ui/button";
import { Dot, type Tone } from "./components/ui/data";
import { Sheet, SheetContent } from "./components/ui/overlay";
import { closeDetail, DetailHost } from "./components/video-detail";
import { api, type Status } from "./lib/api";
import { useMeta, useRun, useStatus } from "./lib/queries";
import { navigate, useRoute } from "./lib/route";
import { loadSession, logout, useSession } from "./lib/session";
import { cn } from "./lib/utils";
import Login from "./pages/login";
import Overview from "./pages/overview";

const pages: { id: string; name: string; icon: ComponentType<{ className?: string }>; Page: ComponentType }[] = [
  { id: "overview", name: "概览", icon: LayoutDashboard, Page: Overview },
  { id: "jobs", name: "任务", icon: ListChecks, Page: lazy(() => import("./pages/jobs")) },
  { id: "videos", name: "影片库", icon: Film, Page: lazy(() => import("./pages/videos")) },
  { id: "libraries", name: "输出库与订阅", icon: FolderTree, Page: lazy(() => import("./pages/libraries")) },
  { id: "strm", name: "strm 管理", icon: FileVideo, Page: lazy(() => import("./pages/strm")) },
  { id: "settings", name: "设置", icon: Settings2, Page: lazy(() => import("./pages/settings")) },
  { id: "logs", name: "日志", icon: ScrollText, Page: lazy(() => import("./pages/logs")) },
];

export function engineState(s: Status | undefined, error: boolean, label: (n: string) => string): { text: string; tone: Tone } {
  if (error) return { text: "服务离线", tone: "err" };
  if (!s) return { text: "加载中", tone: "neutral" };
  const e = s.engine;
  if (e.paused) return { text: "已暂停", tone: "neutral" };
  if (e.blocked_for > 0) return { text: Object.keys(e.blocked).map(label).join("、") + " 被拦截", tone: "warn" };
  if (e.running.length) return { text: `运行中 · ${e.running.length} 个子任务`, tone: "ok" };
  return { text: "空闲", tone: "info" };
}

/** 先看要不要登录：要登录就只显示登录页，控制台的接口一个都不请求。 */
export default function App() {
  const session = useSession();
  useEffect(() => {
    loadSession();
  }, []);
  if (session.status === "loading") return null;
  if (session.status === "offline")
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-bg px-4 text-center">
        <p className="text-sm">连不上 hls2strm 服务。确认它在运行，再重试。</p>
        <Button onClick={loadSession}>
          <RotateCw />
          重试
        </Button>
      </div>
    );
  if (session.status === "login") return <Login />;
  return <Console user={session.required ? session.user : null} />;
}

function Console({ user }: { user: string | null }) {
  const route = useRoute();
  const current = pages.find(p => p.id === route.page) ?? pages[0];
  const [navOpen, setNavOpen] = useState(false);
  const { data: status, isError } = useStatus();
  const meta = useMeta();
  const state = engineState(status, isError, meta.label);

  useEffect(() => {
    document.title = `${current.name} · hls2strm`;
    closeDetail();
  }, [current]);

  const nav = (
    <Nav
      current={current.id}
      status={status}
      user={user}
      onGo={id => {
        navigate(id);
        setNavOpen(false);
      }}
    />
  );

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[212px_minmax(0,1fr)]">
      <aside className="sticky top-0 hidden h-screen flex-col border-r border-line bg-panel lg:flex">{nav}</aside>
      <Sheet open={navOpen} onOpenChange={setNavOpen}>
        <SheetContent side="left" label="导航" className="flex flex-col">
          {nav}
        </SheetContent>
      </Sheet>

      <div className="min-w-0">
        <header className="sticky top-0 z-30 flex h-12 items-center gap-2 border-b border-line bg-bg/90 px-3 backdrop-blur sm:px-5">
          <Button size="icon" variant="ghost" className="lg:hidden" onClick={() => setNavOpen(true)} aria-label="打开导航">
            <MenuIcon />
          </Button>
          <h1 className="shrink-0 text-base font-semibold">{current.name}</h1>
          <div className="mx-2 hidden h-4 w-px bg-line sm:block" />
          <SiteSignals />
          <div className="ml-auto flex shrink-0 items-center gap-2 text-[13px]" title="引擎状态">
            <Dot tone={state.tone} pulse={state.tone === "ok"} />
            <span className="hidden text-muted sm:inline">{state.text}</span>
          </div>
        </header>
        <main className="mx-auto max-w-[1680px] space-y-4 p-3 sm:p-5">
          <Suspense fallback={<div className="py-20 text-center text-sm text-muted">加载中…</div>}>
            <current.Page key={current.id} />
          </Suspense>
        </main>
      </div>

      <DetailHost />
      <PlayerHost />
      <ConfirmHost />
    </div>
  );
}

function Nav({ current, status, user, onGo }: { current: string; status: Status | undefined; user: string | null; onGo: (id: string) => void }) {
  const failed = Object.values(status?.queue ?? {}).reduce((a, q) => a + (q.failed ?? 0), 0);
  const run = useRun();
  const paused = status?.engine.paused;
  return (
    <>
      <div className="flex h-12 items-center gap-2 border-b border-line px-4">
        <span className="flex size-6 items-center justify-center rounded-md bg-accent text-accent-ink">
          <Play className="size-3.5 fill-current" />
        </span>
        <span className="font-semibold tracking-tight">hls2strm</span>
        <span className="ml-auto text-xs text-muted">{status?.version}</span>
      </div>
      <nav className="flex-1 space-y-0.5 overflow-y-auto p-2">
        {pages.map(p => (
          <a
            key={p.id}
            href={`#/${p.id}`}
            onClick={e => {
              e.preventDefault();
              onGo(p.id);
            }}
            aria-current={current === p.id ? "page" : undefined}
            className={cn(
              "flex h-9 items-center gap-2.5 rounded-md px-2.5 text-sm text-muted transition-colors hover:bg-panel-2 hover:text-ink",
              current === p.id && "bg-accent-soft font-medium text-accent hover:bg-accent-soft hover:text-accent",
            )}
          >
            <p.icon className="size-4" />
            {p.name}
            {p.id === "jobs" && failed > 0 && (
              <span className="ml-auto rounded bg-err-soft px-1.5 text-xs text-err" title="活动任务里失败的子任务">
                {failed}
              </span>
            )}
          </a>
        ))}
      </nav>
      <div className="space-y-2 border-t border-line p-3">
        <Button
          size="sm"
          variant={paused ? "primary" : "outline"}
          className="w-full"
          onClick={() =>
            run(() => api.post(`/api/engine/${paused ? "resume" : "pause"}`), { success: paused ? "引擎已恢复" : "引擎已暂停" })
          }
        >
          {paused ? <Play /> : <Pause />}
          {paused ? "恢复全部任务" : "暂停全部任务"}
        </Button>
        <ThemeSwitch />
        {user && (
          <div className="flex items-center gap-2 pt-1 text-[13px]">
            <span className="min-w-0 flex-1 truncate text-muted" title={`已登录：${user}`}>
              {user}
            </span>
            <Button size="sm" variant="quiet" onClick={() => logout()}>
              <LogOut />
              退出
            </Button>
          </div>
        )}
      </div>
    </>
  );
}

type Theme = "system" | "light" | "dark";

function applyTheme(t: Theme) {
  const dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
}

function ThemeSwitch() {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem("hls2strm.theme") as Theme) || "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem("hls2strm.theme", theme);
    } catch {
      /* 存不了就只在本次生效 */
    }
    if (theme !== "system") return;
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const on = () => applyTheme("system");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [theme]);
  const opts: { v: Theme; icon: ComponentType<{ className?: string }>; name: string }[] = [
    { v: "light", icon: Sun, name: "浅色" },
    { v: "system", icon: Monitor, name: "跟随系统" },
    { v: "dark", icon: Moon, name: "深色" },
  ];
  return (
    <div className="flex rounded-md border border-line bg-panel-2 p-0.5" role="radiogroup" aria-label="主题">
      {opts.map(o => (
        <button
          key={o.v}
          type="button"
          role="radio"
          aria-checked={theme === o.v}
          title={o.name}
          onClick={() => setTheme(o.v)}
          className={cn(
            "flex h-6 flex-1 items-center justify-center rounded-[5px] text-muted hover:text-ink",
            theme === o.v && "bg-panel text-ink shadow-[0_0_0_1px_var(--line)]",
          )}
        >
          <o.icon className="size-3.5" />
        </button>
      ))}
    </div>
  );
}
