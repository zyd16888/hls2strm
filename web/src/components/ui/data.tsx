import type { ComponentProps, ReactNode } from "react";
import { cn } from "@/lib/utils";

export type Tone = "neutral" | "ok" | "warn" | "err" | "info" | "accent";

const toneClass: Record<Tone, string> = {
  neutral: "bg-panel-2 text-muted border-line",
  ok: "bg-ok-soft text-ok border-transparent",
  warn: "bg-warn-soft text-warn border-transparent",
  err: "bg-err-soft text-err border-transparent",
  info: "bg-info-soft text-info border-transparent",
  accent: "bg-accent-soft text-accent border-transparent",
};

/** 小标签：状态、站点、库名。 */
export function Chip({ tone = "neutral", className, ...props }: ComponentProps<"span"> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 max-w-full items-center gap-1 truncate whitespace-nowrap rounded border px-1.5 text-xs leading-none [&_svg]:size-3",
        toneClass[tone],
        className,
      )}
      {...props}
    />
  );
}

/** 状态灯：圆点 + 文字。 */
export function Dot({ tone = "neutral", pulse, className }: { tone?: Tone; pulse?: boolean; className?: string }) {
  const color = {
    neutral: "bg-muted/50",
    ok: "bg-ok",
    warn: "bg-warn",
    err: "bg-err",
    info: "bg-info",
    accent: "bg-accent",
  }[tone];
  return (
    <span className={cn("relative inline-flex size-2 shrink-0", className)}>
      {pulse && <span className={cn("absolute inset-0 rounded-full opacity-60 motion-safe:animate-ping", color)} />}
      <span className={cn("relative inline-flex size-2 rounded-full", color)} />
    </span>
  );
}

/** 页面里的一块内容：标题栏 + 正文。 */
export function Panel({
  title,
  actions,
  children,
  className,
  bodyClassName,
  id,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  id?: string;
}) {
  return (
    <section id={id} className={cn("min-w-0 rounded-lg border border-line bg-panel", className)}>
      {(title || actions) && (
        <header className="flex min-h-11 flex-wrap items-center gap-2 border-b border-line px-4 py-2">
          {title && <h2 className="text-[15px] font-semibold">{title}</h2>}
          {actions && <div className="ml-auto flex flex-wrap items-center gap-1.5">{actions}</div>}
        </header>
      )}
      <div className={cn("p-4", bodyClassName)}>{children}</div>
    </section>
  );
}

/** 提示条：被拦截、文件缺失这类需要处理的状况。 */
export function Notice({ tone = "warn", children, actions }: { tone?: Tone; children: ReactNode; actions?: ReactNode }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border px-4 py-2.5 text-sm", toneClass[tone])}>
      <div className="min-w-0 flex-1 text-ink">{children}</div>
      {actions && <div className="flex flex-wrap gap-1.5">{actions}</div>}
    </div>
  );
}

export function Table({ className, ...props }: ComponentProps<"table">) {
  return (
    <div className="scroll-thin -mx-px overflow-x-auto">
      <table className={cn("w-full border-collapse text-sm", className)} {...props} />
    </div>
  );
}

export function Th({ className, ...props }: ComponentProps<"th">) {
  return (
    <th
      className={cn("h-8 whitespace-nowrap border-b border-line px-2.5 text-left text-xs font-medium text-muted first:pl-0 last:pr-0", className)}
      {...props}
    />
  );
}

export function Td({ className, ...props }: ComponentProps<"td">) {
  return <td className={cn("border-b border-line px-2.5 py-2 align-middle first:pl-0 last:pr-0", className)} {...props} />;
}

export function EmptyRow({ cols, children }: { cols: number; children: ReactNode }) {
  return (
    <tr>
      <td colSpan={cols} className="py-8 text-center text-sm text-muted">
        {children}
      </td>
    </tr>
  );
}

export function Progress({ value, tone = "accent", className }: { value: number; tone?: Tone; className?: string }) {
  const color = tone === "ok" ? "bg-ok" : tone === "warn" ? "bg-warn" : tone === "err" ? "bg-err" : "bg-accent";
  return (
    <div className={cn("h-1.5 overflow-hidden rounded-full bg-panel-2 ring-1 ring-inset ring-line", className)}>
      <div className={cn("h-full rounded-full transition-[width] duration-500", color)} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </div>
  );
}

/** 键值列表。 */
export function KV({ items, className }: { items: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-[max-content_1fr] gap-x-5 gap-y-1.5 text-sm", className)}>
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** 番号标签（窄体大写，像书脊标签）。 */
export function Code({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("code-label text-[15px] leading-none", className)}>{children}</span>;
}

export function Mono({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("break-all font-mono text-[12.5px]", className)} {...props} />;
}

/** 收起的说明文字。 */
export function Help({ summary = "说明", children }: { summary?: string; children: ReactNode }) {
  return (
    <details className="group text-[13px] text-muted">
      <summary className="cursor-pointer select-none text-muted hover:text-ink">{summary}</summary>
      <div className="mt-2 space-y-2 leading-relaxed">{children}</div>
    </details>
  );
}
