import { ChevronDown } from "lucide-react";
import { Switch as SwitchPrimitive } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "@/lib/utils";

const control =
  "w-full min-w-0 rounded-md border border-line bg-panel text-sm text-ink placeholder:text-muted/80 " +
  "focus-visible:border-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/20 " +
  "disabled:cursor-not-allowed disabled:opacity-50";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input spellCheck={false} className={cn(control, "h-8 px-2.5", className)} {...props} />;
}

export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return <textarea spellCheck={false} className={cn(control, "min-h-20 px-2.5 py-1.5 font-mono text-[13px]", className)} {...props} />;
}

/** 原生下拉框：选项少、要在手机上好用，不用自绘。 */
export function Select({ className, children, ...props }: ComponentProps<"select">) {
  return (
    <span className={cn("relative inline-flex min-w-0", className)}>
      <select className={cn(control, "h-8 cursor-pointer appearance-none py-0 pl-2.5 pr-7")} {...props}>
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2 top-1/2 size-3.5 -translate-y-1/2 text-muted" />
    </span>
  );
}

export function Switch({ className, ...props }: ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        "inline-flex h-[18px] w-8 shrink-0 cursor-pointer items-center rounded-full border border-transparent bg-line transition-colors " +
          "data-[state=checked]:bg-accent disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb className="block size-3.5 translate-x-0.5 rounded-full bg-white shadow-sm transition-transform data-[state=checked]:translate-x-[15px]" />
    </SwitchPrimitive.Root>
  );
}

/** 表单项：上面是名称，下面是控件和说明。 */
export function Field({
  label,
  hint,
  className,
  children,
}: {
  label: ReactNode;
  hint?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <label className={cn("flex min-w-0 flex-col gap-1", className)}>
      <span className="text-[13px] text-muted">{label}</span>
      {children}
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </label>
  );
}

export function Check({
  checked,
  onChange,
  disabled,
  children,
  className,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("inline-flex cursor-pointer items-center gap-1.5 text-sm", disabled && "cursor-not-allowed opacity-50", className)}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={e => onChange(e.target.checked)} />
      {children}
    </label>
  );
}

/** 分段选择：几个互斥选项并排。 */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  className,
  size = "md",
}: {
  value: T;
  options: { value: T; label: ReactNode; title?: string }[];
  onChange: (v: T) => void;
  className?: string;
  size?: "sm" | "md";
}) {
  return (
    <div role="radiogroup" className={cn("inline-flex w-fit max-w-full rounded-md border border-line bg-panel-2 p-0.5", className)}>
      {options.map(o => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          title={o.title}
          onClick={() => onChange(o.value)}
          className={cn(
            "inline-flex items-center gap-1 rounded-[5px] px-2.5 text-[13px] text-muted transition-colors hover:text-ink [&_svg]:size-3.5",
            size === "sm" ? "h-6" : "h-7",
            value === o.value && "bg-panel text-ink shadow-[0_0_0_1px_var(--line)]",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
