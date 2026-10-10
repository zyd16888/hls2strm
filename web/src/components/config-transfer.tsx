import { Download, Upload } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, Check, Textarea } from "@/components/ui/form";
import { Dialog, DialogContent } from "@/components/ui/overlay";
import { api } from "@/lib/api";
import { fmtScheduledTime } from "@/lib/format";
import { useRun } from "@/lib/queries";

type ImportAction = "create" | "reuse" | "update";
interface Change { field: string; before: unknown; after: unknown }
interface ImportResult {
  libraries_created: number; subscriptions_created: number;
  libraries_updated: number; subscriptions_updated: number;
}

interface Preview {
  preview_token: string;
  libraries: { key: string; id: number; name: string; existing_name: string | null;
    root: string; external_root: string; action: ImportAction; changes: Change[];
    source_names: string[]; exclude_names: string[]; preserved_source_names: string[] }[];
  subscriptions: { name: string; site: string; source: string; library_name: string; interval: number;
    cron: string | null; timezone: string; scheduled_at: number | null;
    id: number | null; existing_name: string | null; changes: Change[];
    enabled: boolean; detail: boolean; initial_full: boolean; action: ImportAction }[];
  warnings: string[];
}

const actionLabel = { create: "新建", reuse: "复用", update: "更新" };
const fieldLabel: Record<string, string> = {
  name: "名称", dir: "收件目录", external_dir: "整理目录", path_template: "路径模板",
  versions: "多画质版本", rule: "规则", sources: "来源库", excludes: "排除库",
  site: "站点", source: "列表地址", sort: "排序", library_id: "输出库", detail: "抓详情",
  interval: "旧周期（分钟）", cron: "Cron", timezone: "时区",
  stop_after_known: "连续已知阈值", max_pages: "单次最多页数",
};
const showValue = (value: unknown) => {
  if (value === null || value === "") return "（空）";
  if (Array.isArray(value)) return value.length ? value.join("、") : "（无）";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
};

function Changes({ changes }: { changes: Change[] }) {
  if (!changes.length) return null;
  return <dl className="mt-2 space-y-2 rounded bg-tint p-2 text-xs" aria-label="更新差异">
    {changes.map(change => <div key={change.field}>
      <dt className="font-medium">{fieldLabel[change.field] ?? change.field}</dt>
      <dd className="break-all text-muted">原：{showValue(change.before)}</dd>
      <dd className="break-all">新：{showValue(change.after)}</dd>
    </div>)}
  </dl>;
}

export function ConfigTransfer() {
  const run = useRun();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [activate, setActivate] = useState(false);
  const [updateExisting, setUpdateExisting] = useState(false);
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
      setPreview(await api.post<Preview>("/api/configuration/preview", {
        config, activate_subscriptions: activate, update_existing: updateExisting,
      }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };
  const apply = async () => {
    if (!preview) return;
    setBusy(true); setError("");
    const result = await run(() => api.post<ImportResult>(
      "/api/configuration/import", {
        config: JSON.parse(draft.replace(/^\uFEFF/, "")), activate_subscriptions: activate,
        update_existing: updateExisting,
        preview_token: preview.preview_token,
      }), {
        success: r => `导入完成：输出库新建 ${r.libraries_created} / 更新 ${r.libraries_updated}，订阅新建 ${r.subscriptions_created} / 更新 ${r.subscriptions_updated}`,
        invalidate: [["libraries"], ["subscriptions"], ["status"]],
      });
    setBusy(false);
    if (result) { setOpen(false); setDraft(""); setPreview(null); }
    else { setPreview(null); setError("导入未完成，请检查错误提示并重新预览。"); }
  };

  return <>
    <Button size="sm" disabled={busy} onClick={exportFile}><Download />导出配置</Button>
    <Button size="sm" disabled={busy} onClick={() => {
      setOpen(true); setPreview(null); setError(""); setActivate(false); setUpdateExisting(false);
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
          <p className="text-[13px] text-muted">包含输出库、来源/排除关系和订阅 cron / 时区，不包含全局设置、密钥、影片和历史任务。默认只复用相同配置；允许更新后，确认前会展示每项差异。</p>
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
          <Check checked={updateExisting} disabled={busy} onChange={v => { setUpdateExisting(v); setPreview(null); setError(""); }}>
            允许更新现有配置（预览差异，确认后更新）
          </Check>
          <p className="text-xs text-muted">输出库按名称或同一组收件/整理根目录匹配；订阅按名称或同站来源及输出库匹配。更新保留 ID、进度和现有来源库关系；根目录变化、订阅来源变化或匹配不唯一会阻断。</p>
          {error && <p role="alert" className="break-words text-sm text-err">{error}</p>}
          {preview && <section aria-label="导入预览" className="space-y-3 text-sm">
            <p className="font-medium">确认范围：{preview.libraries.length} 个输出库、{preview.subscriptions.length} 个订阅</p>
            <p className="text-xs text-muted">输出库：新建 {preview.libraries.filter(l => l.action === "create").length} / 更新 {preview.libraries.filter(l => l.action === "update").length} / 复用 {preview.libraries.filter(l => l.action === "reuse").length}；订阅：新建 {preview.subscriptions.filter(s => s.action === "create").length} / 更新 {preview.subscriptions.filter(s => s.action === "update").length} / 复用 {preview.subscriptions.filter(s => s.action === "reuse").length}</p>
            {preview.libraries.map(lib => <div key={lib.key} className="rounded border border-line p-3">
              <p>{actionLabel[lib.action]}输出库 · {lib.name}</p>
              {lib.action !== "create" && <p className="text-xs text-muted">保留现有库 #{lib.id} · {lib.existing_name}</p>}
              <p className="break-all text-xs text-muted">收件/输出：{lib.root}</p>
              {lib.external_root && <p className="break-all text-xs text-muted">外部整理：{lib.external_root}</p>}
              {!!lib.source_names.length && <p className="text-xs text-muted">来源库：{lib.source_names.join("、")}</p>}
              {!!lib.exclude_names.length && <p className="text-xs text-muted">排除：{lib.exclude_names.join("、")}</p>}
              {!!lib.preserved_source_names.length && <p className="text-xs text-muted">保留既有来源库：{lib.preserved_source_names.join("、")}</p>}
              <Changes changes={lib.changes} />
            </div>)}
            {preview.subscriptions.map(sub => <div key={sub.name} className="rounded border border-line p-3">
              <p>{actionLabel[sub.action]}订阅 · {sub.name} → {sub.library_name}</p>
              {sub.id !== null && <p className="text-xs text-muted">保留现有订阅 #{sub.id} · {sub.existing_name}，启用状态和抓取进度保持原样</p>}
              <p className="break-all text-xs text-muted">{sub.site} · {sub.source}</p>
              <p className="text-xs text-muted">{sub.enabled ? "启用" : "停用"} · {sub.cron ?? (sub.interval ? `每 ${sub.interval} 分钟（旧配置）` : "仅手动")}{sub.cron === "" && "仅手动"}{sub.cron && `（${sub.timezone}）`} · {sub.detail ? "抓详情" : "仅列表"}
                {sub.action === "create" && ` · ${sub.initial_full ? "需要首轮全量" : "直接增量"}`}</p>
              {sub.scheduled_at && <p className="text-xs text-muted">下次计划时间：{fmtScheduledTime(sub.scheduled_at, sub.timezone)}（启用并完成首轮后生效）</p>}
              <Changes changes={sub.changes} />
            </div>)}
            <ul className="list-disc space-y-1 pl-5 text-xs text-muted">{preview.warnings.map(w => <li key={w}>{w}</li>)}</ul>
          </section>}
        </div>
      </DialogContent>
    </Dialog>
  </>;
}
