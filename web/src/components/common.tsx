import { Check as CheckIcon, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Copy } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import type { Source, SourceLine } from "@/lib/api";
import { fmtDur, fmtNum } from "@/lib/format";
import { copyText } from "@/lib/utils";
import { Button } from "./ui/button";
import type { Tone } from "./ui/data";
import { Select } from "./ui/form";

export function CopyButton({ text, label = "复制", size = "sm" }: { text: string; label?: string; size?: "sm" | "icon-sm" }) {
  const [done, setDone] = useState(false);
  const onClick = async () => {
    if (await copyText(text)) {
      setDone(true);
      setTimeout(() => setDone(false), 1200);
    } else {
      toast.error("复制失败，请手动选中复制");
    }
  };
  return (
    <Button size={size} variant="outline" onClick={onClick} title={text} aria-label={label}>
      {done ? <CheckIcon className="text-ok" /> : <Copy />}
      {size === "sm" && (done ? "已复制" : label)}
    </Button>
  );
}

/** 源（或线路）现在的播放地址状态。 */
export function streamState(s: Source | SourceLine, now = Date.now() / 1000): { text: string; tone: Tone } {
  if (s.status === "gone") return { text: "已下架", tone: "err" };
  if (s.status === "disabled") return { text: "已禁用", tone: "neutral" };
  if (s.cooldown_until > now) return { text: `失败冷却 ${fmtDur(s.cooldown_until - now)}`, tone: "warn" };
  if (!s.stream_url) return { text: "未缓存", tone: "neutral" };
  if (!s.expires_stream) return { text: "长期有效", tone: "ok" };
  if (!s.stream_expires) return { text: "有效期未知", tone: "warn" };
  const left = s.stream_expires - now;
  if (left <= 0) return { text: "已过期", tone: "neutral" };
  return { text: `剩 ${fmtDur(left)}`, tone: left > 3600 ? "ok" : "warn" };
}

export function Pager({
  page,
  size,
  total,
  onPage,
  onSize,
  sizes = [24, 50, 100, 200],
}: {
  page: number;
  size: number;
  total: number;
  onPage: (p: number) => void;
  onSize?: (s: number) => void;
  sizes?: number[];
}) {
  const pages = Math.max(1, Math.ceil(total / size));
  const [draft, setDraft] = useState<string | null>(null);
  const go = (p: number) => onPage(Math.min(pages, Math.max(1, p)));
  return (
    <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
      <span>
        共 <b className="text-ink">{fmtNum(total)}</b> 条
      </span>
      {onSize && (
        <Select value={size} onChange={e => onSize(Number(e.target.value))} aria-label="每页条数" className="w-[92px]">
          {sizes.map(s => (
            <option key={s} value={s}>
              每页 {s}
            </option>
          ))}
        </Select>
      )}
      <div className="ml-auto flex items-center gap-1">
        <Button size="icon-sm" variant="ghost" disabled={page <= 1} onClick={() => go(1)} aria-label="第一页">
          <ChevronsLeft />
        </Button>
        <Button size="icon-sm" variant="ghost" disabled={page <= 1} onClick={() => go(page - 1)} aria-label="上一页">
          <ChevronLeft />
        </Button>
        <span className="flex items-center gap-1">
          第
          <input
            aria-label="页码"
            className="h-7 w-12 rounded-md border border-line bg-panel text-center text-ink"
            value={draft ?? String(page)}
            onChange={e => setDraft(e.target.value.replace(/\D/g, ""))}
            onBlur={() => {
              if (draft) go(Number(draft));
              setDraft(null);
            }}
            onKeyDown={e => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          />
          / {fmtNum(pages)} 页
        </span>
        <Button size="icon-sm" variant="ghost" disabled={page >= pages} onClick={() => go(page + 1)} aria-label="下一页">
          <ChevronRight />
        </Button>
        <Button size="icon-sm" variant="ghost" disabled={page >= pages} onClick={() => go(pages)} aria-label="最后一页">
          <ChevronsRight />
        </Button>
      </div>
    </div>
  );
}

/** 错误信息里的「快照 xxx.html」变成可点的链接。 */
export function ErrorText({ msg }: { msg: string }) {
  if (!msg) return null;
  const m = msg.match(/快照 ([\w.-]+\.html)/);
  if (!m) return <span className="break-all text-err">{msg}</span>;
  const [before, after] = msg.split(m[0]);
  return (
    <span className="break-all text-err">
      {before}快照{" "}
      <a className="underline" target="_blank" rel="noreferrer" href={`/api/snapshots/${encodeURIComponent(m[1])}`}>
        {m[1]}
      </a>
      {after}
    </span>
  );
}
