import { useEffect, useRef } from "react";
import type { LogItem } from "@/lib/api";
import { fmtClock, fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const levelClass: Record<string, string> = {
  DEBUG: "text-muted",
  INFO: "text-ok",
  WARNING: "text-warn",
  ERROR: "text-err",
  CRITICAL: "text-err",
};

/** 日志行；follow 时新日志进来自动滚到底（用户往上翻看时不打扰）。 */
export function LogLines({
  items,
  follow = true,
  short = false,
  className,
}: {
  items: LogItem[];
  follow?: boolean;
  short?: boolean;
  className?: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useEffect(() => {
    const el = box.current;
    if (el && follow && stick.current) el.scrollTop = el.scrollHeight;
  }, [items, follow]);
  return (
    <div
      ref={box}
      onScroll={e => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
      className={cn("scroll-thin overflow-auto rounded-md bg-panel-2 px-3 py-2 font-mono text-[12.5px] leading-[1.65]", className)}
    >
      {items.length === 0 && <div className="py-6 text-center font-sans text-[13px] text-muted">还没有日志</div>}
      {items.map(l => (
        <div key={l.id} className="whitespace-pre-wrap break-all">
          <span className="text-muted">{short ? fmtClock(l.ts) : fmtTime(l.ts)}</span>{" "}
          <span className={cn("inline-block w-[8ch]", levelClass[l.level])}>{l.level}</span>
          {!short && <span className="mr-2 text-info">{l.name}</span>}
          {l.msg}
        </div>
      ))}
    </div>
  );
}
