import { Download, Upload } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, Check, Textarea } from "@/components/ui/form";
import { Dialog, DialogContent } from "@/components/ui/overlay";
import { api } from "@/lib/api";
import { fmtScheduledTime } from "@/lib/format";
import { useRun } from "@/lib/queries";

interface Preview {
  preview_token: string;
  libraries: { key: string; name: string; root: string; external_root: string; action: "create" | "reuse";
    source_names: string[]; exclude_names: string[] }[];
  subscriptions: { name: string; site: string; source: string; library_name: string; interval: number;
    cron: string | null; timezone: string; scheduled_at: number | null;
    enabled: boolean; detail: boolean; initial_full: boolean; action: "create" | "reuse" }[];
  warnings: string[];
}

const actionLabel = { create: "新建", reuse: "复用" };

export function ConfigTransfer() {
  const run = useRun();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [activate, setActivate] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const change = (text: string) => { setDraft(text); setPreview(null); setError(""); };
  const exportFile = async () => {
    setBusy(true);
    await run(async () => {
      const config = await api.get<unknown>("/api/configuration/export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(config, null, 2) + "\n"], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "hls2strm-config.json";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, { invalidate: [] });
    setBusy(false);
  };
  const validate = async () => {
    setBusy(true); setError(""); setPreview(null);
    try {
      const config: unknown = JSON.parse(draft.replace(/^\uFEFF/, ""));
      setPreview(await api.post<Preview>("/api/configuration/preview", { config, activate_subscriptions: activate }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };
  const apply = async () => {
    if (!preview) return;
    setBusy(true); setError("");
    const result = await run(() => api.post<{ libraries_created: number; subscriptions_created: number }>(
      "/api/configuration/import", {
        config: JSON.parse(draft.replace(/^\uFEFF/, "")), activate_subscriptions: activate,
        preview_token: preview.preview_token,
      }), {
        success: r => `导入完成：新建 ${r.libraries_created} 个输出库、${r.subscriptions_created} 个订阅`,
        invalidate: [["libraries"], ["subscriptions"], ["status"]],
      });
    setBusy(false);
    if (result) { setOpen(false); setDraft(""); setPreview(null); }
    else { setPreview(null); setError("导入未完成，请检查错误提示并重新预览。"); }
  };

  return <>
    <Button size="sm" disabled={busy} onClick={exportFile}><Download />导出配置</Button>
    <Button size="sm" disabled={busy} onClick={() => {
      setOpen(true); setPreview(null); setError(""); setActivate(false);
    }}><Upload />导入配置</Button>
    <Dialog open={open} onOpenChange={value => { if (!busy) setOpen(value); }}>
      <DialogContent title="导入输出库与订阅配置" className="max-w-3xl"
        description="选择文件或粘贴 JSON，可直接修改内容。预览不会保存，确认导入后才生效。"
        footer={<>
          <Button disabled={busy} onClick={() => setOpen(false)}>取消</Button>
          <Button disabled={busy || !draft.trim()} onClick={validate}>校验并预览</Button>
          <Button variant="primary" disabled={busy || !preview} onClick={apply}>{busy ? "处理中…" : "确认导入"}</Button>
        </>}>
        <div className="space-y-4">
          <p className="text-[13px] text-muted">包含输出库、来源/排除关系和订阅 cron / 时区，不包含全局设置、密钥、影片和历史任务。同名且相同的配置复用，冲突时停止导入。</p>
          <Field label="配置文件（JSON，最大 2 MB）">
            <input type="file" accept=".json,application/json" disabled={busy} className="w-full text-sm"
              onChange={async e => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (!file) return;
                setPreview(null); setError("");
                if (file.size > 2 * 1024 * 1024) { setError("文件超过 2 MB，请检查是否选择了配置文件。"); return; }
                setBusy(true);
                try { change(await file.text()); }
                catch { setError("读取文件失败，请重新选择。"); }
                finally { setBusy(false); }
              }} />
          </Field>
          <Field label="配置内容（可编辑）">
            <Textarea className="h-64" value={draft} disabled={busy} onChange={e => change(e.target.value)}
              placeholder={'{"format":"hls2strm-library-config","version":1,"libraries":[],"subscriptions":[]}'} />
          </Field>
          <Check checked={activate} disabled={busy} onChange={v => { setActivate(v); setPreview(null); }}>
            按文件中的 enabled 启用新订阅
          </Check>
          <p className="text-xs text-muted">不勾选时新订阅全部停用，之后可逐个运行首轮全量、再开启定时增量。已有订阅的启用状态和进度保持原样。</p>
          {error && <p role="alert" className="break-words text-sm text-err">{error}</p>}
          {preview && <section aria-label="导入预览" className="space-y-3 text-sm">
            <p className="font-medium">确认范围：{preview.libraries.length} 个输出库、{preview.subscriptions.length} 个订阅</p>
            {preview.libraries.map(lib => <div key={lib.key} className="rounded border border-line p-3">
              <p>{actionLabel[lib.action]}输出库 · {lib.name}</p>
              <p className="break-all text-xs text-muted">收件/输出：{lib.root}</p>
              {lib.external_root && <p className="break-all text-xs text-muted">外部整理：{lib.external_root}</p>}
              {!!lib.source_names.length && <p className="text-xs text-muted">来源库：{lib.source_names.join("、")}</p>}
              {!!lib.exclude_names.length && <p className="text-xs text-muted">排除：{lib.exclude_names.join("、")}</p>}
            </div>)}
            {preview.subscriptions.map(sub => <div key={sub.name} className="rounded border border-line p-3">
              <p>{actionLabel[sub.action]}订阅 · {sub.name} → {sub.library_name}</p>
              <p className="break-all text-xs text-muted">{sub.site} · {sub.source}</p>
              <p className="text-xs text-muted">{sub.enabled ? "启用" : "停用"} · {sub.cron ?? (sub.interval ? `每 ${sub.interval} 分钟（旧配置）` : "仅手动")}{sub.cron === "" && "仅手动"}{sub.cron && `（${sub.timezone}）`} · {sub.detail ? "抓详情" : "仅列表"}
                {sub.action === "create" && ` · ${sub.initial_full ? "需要首轮全量" : "直接增量"}`}</p>
              {sub.scheduled_at && <p className="text-xs text-muted">下次计划时间：{fmtScheduledTime(sub.scheduled_at, sub.timezone)}（启用并完成首轮后生效）</p>}
            </div>)}
            <ul className="list-disc space-y-1 pl-5 text-xs text-muted">{preview.warnings.map(w => <li key={w}>{w}</li>)}</ul>
          </section>}
        </div>
      </DialogContent>
    </Dialog>
  </>;
}
