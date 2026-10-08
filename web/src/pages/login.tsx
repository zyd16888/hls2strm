import { Eye, EyeOff, LogIn, Play } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Check, Field, Input } from "@/components/ui/form";
import { login } from "@/lib/session";

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(username.trim(), password, remember);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4 py-10">
      <main className="relative w-full max-w-[380px] overflow-hidden rounded-xl border border-line bg-panel shadow-[0_24px_60px_-24px_rgb(0_0_0/0.35)]">
        {/* 左边一条胶片齿孔：这个工具做的就是把站点上的影片变成媒体库里能播的条目 */}
        <div aria-hidden className="film-edge absolute inset-y-0 left-0 w-3.5" />
        <form onSubmit={submit} className="space-y-5 py-8 pl-10 pr-8">
          <header className="space-y-3">
            <span className="flex size-9 items-center justify-center rounded-lg bg-accent text-accent-ink">
              <Play className="size-4 fill-current" />
            </span>
            <div>
              <h1 className="text-xl font-semibold tracking-tight">登录 hls2strm</h1>
              <p className="mt-1 text-[13px] leading-relaxed text-muted">把站点上的影片整理成 Emby、Jellyfin 能直接播的 strm。</p>
            </div>
          </header>

          <Field label="用户名">
            <Input value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" autoFocus required name="username" className="h-9" />
          </Field>
          <Field label="密码">
            <div className="relative">
              <Input
                type={show ? "text" : "password"}
                value={password}
                onChange={e => setPassword(e.target.value)}
                autoComplete="current-password"
                required
                name="password"
                className="h-9 pr-9"
              />
              <button
                type="button"
                onClick={() => setShow(v => !v)}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-muted hover:text-ink"
                aria-label={show ? "隐藏密码" : "显示密码"}
              >
                {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
              </button>
            </div>
          </Field>
          <Check checked={remember} onChange={setRemember}>
            记住我（30 天内不用再登录）
          </Check>

          <p role="alert" aria-live="polite" className="min-h-5 text-[13px] text-err">
            {error}
          </p>
          <Button type="submit" variant="primary" className="h-9 w-full" disabled={busy || !username.trim() || !password}>
            <LogIn />
            {busy ? "登录中…" : "登录"}
          </Button>
          <p className="text-xs leading-relaxed text-muted">用户名和密码是部署时设置的 HLS2STRM_UI_USER（默认 admin）和 HLS2STRM_UI_PASSWORD。</p>
        </form>
      </main>
    </div>
  );
}
