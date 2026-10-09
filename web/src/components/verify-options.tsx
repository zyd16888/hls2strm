import { useState } from "react";
import { Check } from "./ui/form";

export const defaultVerifyOptions = { repair: true, covers: true, details: false, force_external: false };
export type VerifyValues = typeof defaultVerifyOptions;

export function verifyParams(v: VerifyValues) {
  return { repair: v.repair, covers: v.repair && v.covers, detail: v.repair && v.details, force_external: v.repair && v.force_external };
}

export const verifyHint = "检查 strm 的存在和播放地址、nfo 和封面。补回时在本地重写 strm、nfo，封面先尝试从别的库硬链接，再下载。没详情的影片可排队抓详情（外部整理库不抓）。外部整理库按内容查找，找到只更新路径，两边都找不到才写回收件目录；默认跳过不存在或为空的外部整理目录，请先检查挂载。";

export function VerifyOptions({ value, onChange }: { value: VerifyValues; onChange: (v: VerifyValues) => void }) {
  const set = (patch: Partial<VerifyValues>) => onChange({ ...value, ...patch });
  return (
    <div className="flex min-h-8 flex-wrap items-center gap-x-4 gap-y-2">
      <Check checked={value.repair} onChange={repair => set({ repair })}>发现问题就补回</Check>
      <Check checked={value.covers} disabled={!value.repair} onChange={covers => set({ covers })}>补封面（要下载）</Check>
      <Check checked={value.details} disabled={!value.repair} onChange={details => set({ details })}>没详情的抓详情（要联网）</Check>
      <Check checked={value.force_external} disabled={!value.repair} onChange={force_external => set({ force_external })}>外部整理目录不在或是空的也写回</Check>
    </div>
  );
}

export function VerifyPrompt({ onChange }: { onChange: (v: VerifyValues) => void }) {
  const [value, setValue] = useState(defaultVerifyOptions);
  return <div className="space-y-4"><p>{verifyHint}</p><VerifyOptions value={value} onChange={v => { setValue(v); onChange(v); }} /></div>;
}
