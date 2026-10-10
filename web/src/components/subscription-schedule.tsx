import { useEffect, useState } from "react";
import { Field, Input } from "@/components/ui/form";
import { api, type Subscription } from "@/lib/api";
import { fmtScheduledTime } from "@/lib/format";

export function nextSubscriptionTime(sub: Subscription): string {
  if (!sub.enabled) return "已停用";
  if (!sub.cron && (sub.cron !== null || !sub.interval)) return "仅手动";
  if (!sub.initialized) return "等待首轮全量";
  return sub.next_run_at ? fmtScheduledTime(sub.next_run_at, sub.timezone) : "—";
}

export function SubscriptionSchedule({ cron, timezone, onChange, onValidity }: {
  cron: string; timezone: string;
  onChange: (cron: string, timezone: string) => void;
  onValidity: (valid: boolean) => void;
}) {
  const [preview, setPreview] = useState<{ next_run_at: number | null; timezone: string } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    setPreview(null); setError(""); onValidity(false);
    const timer = setTimeout(async () => {
      try {
        const response = await api.post<{ next_run_at: number | null; timezone: string }>("/api/subscriptions/schedule-preview", { cron, timezone });
        if (!cancelled) { setPreview(response); onValidity(true); }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    }, 350);
    return () => { clearTimeout(timer); cancelled = true; };
  }, [cron, timezone, onValidity]);
  return <div className="space-y-3 sm:col-span-2">
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label="Cron 表达式" hint="分 时 日 月 周；留空仅手动">
        <Input value={cron} onChange={e => onChange(e.target.value, timezone)} placeholder="15 3 * * *" className="font-mono" />
      </Field>
      <Field label="时区" hint="IANA 时区名称">
        <Input value={timezone} onChange={e => onChange(cron, e.target.value)} placeholder="Asia/Shanghai" />
      </Field>
    </div>
    <p className="text-xs text-muted">例如 <code>15 3 * * *</code> 每天 03:15；<code>15 3,15 * * *</code> 每天 03:15 和 15:15。</p>
    {error ? <p role="alert" className="text-sm text-err">{error}</p> : <p className="text-sm text-muted">
      下次计划时间：{preview ? (preview.next_run_at ? `${fmtScheduledTime(preview.next_run_at, preview.timezone)}（${preview.timezone}）` : "仅手动运行") : "校验中…"}
      <span className="block text-xs">启用并完成首轮全量后生效；保存不会立即抓取。</span>
    </p>}
  </div>;
}
