import { X } from "lucide-react";
import { Dialog as D, DropdownMenu as M, Popover as P } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "@/lib/utils";

// ---- 对话框 ----

export const Dialog = D.Root;
export const DialogTrigger = D.Trigger;
export const DialogClose = D.Close;

export function DialogContent({
  title,
  description,
  className,
  children,
  footer,
  ...props
}: Omit<ComponentProps<typeof D.Content>, "title"> & { title: ReactNode; description?: ReactNode; footer?: ReactNode }) {
  return (
    <D.Portal>
      <D.Overlay className="anim-fade fixed inset-0 z-50 bg-black/45" />
      <D.Content
        className={cn(
          "anim-pop fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100vh-32px)] w-[calc(100vw-32px)] max-w-lg -translate-x-1/2 -translate-y-1/2 flex-col " +
            "rounded-lg border border-line bg-panel shadow-[0_18px_50px_-12px_rgb(0_0_0/0.35)] focus:outline-none",
          className,
        )}
        {...props}
      >
        <div className="flex items-start gap-3 border-b border-line px-5 py-3.5">
          <div className="min-w-0 flex-1">
            <D.Title className="text-base font-semibold">{title}</D.Title>
            {description ? (
              <D.Description className="mt-1 text-[13px] text-muted">{description}</D.Description>
            ) : (
              <D.Description className="sr-only">{typeof title === "string" ? title : ""}</D.Description>
            )}
          </div>
          <D.Close className="-mr-1 rounded p-1 text-muted hover:bg-panel-2 hover:text-ink" aria-label="关闭">
            <X className="size-4" />
          </D.Close>
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </D.Content>
    </D.Portal>
  );
}

// ---- 侧边抽屉 ----

export const Sheet = D.Root;

export function SheetContent({
  side = "right",
  className,
  children,
  label,
  ...props
}: ComponentProps<typeof D.Content> & { side?: "left" | "right"; label: string }) {
  return (
    <D.Portal>
      <D.Overlay className="anim-fade fixed inset-0 z-50 bg-black/40" />
      <D.Content
        aria-describedby={undefined}
        className={cn(
          "fixed inset-y-0 z-50 flex w-full flex-col bg-panel shadow-[0_0_60px_-10px_rgb(0_0_0/0.4)] focus:outline-none",
          side === "right" ? "anim-right right-0 border-l border-line sm:max-w-2xl" : "anim-left left-0 max-w-[260px] border-r border-line",
          className,
        )}
        {...props}
      >
        <D.Title className="sr-only">{label}</D.Title>
        {children}
      </D.Content>
    </D.Portal>
  );
}

export const SheetClose = D.Close;

// ---- 浮层 ----

export const Popover = P.Root;
export const PopoverTrigger = P.Trigger;

export function PopoverContent({ className, align = "start", sideOffset = 6, ...props }: ComponentProps<typeof P.Content>) {
  return (
    <P.Portal>
      <P.Content
        align={align}
        sideOffset={sideOffset}
        className={cn(
          "anim-fade z-50 rounded-lg border border-line bg-panel p-3 shadow-[0_12px_32px_-8px_rgb(0_0_0/0.28)] focus:outline-none",
          className,
        )}
        {...props}
      />
    </P.Portal>
  );
}

// ---- 菜单 ----

export const Menu = M.Root;
export const MenuTrigger = M.Trigger;

export function MenuContent({ className, align = "end", ...props }: ComponentProps<typeof M.Content>) {
  return (
    <M.Portal>
      <M.Content
        align={align}
        sideOffset={4}
        className={cn(
          "anim-fade z-50 min-w-36 rounded-lg border border-line bg-panel p-1 shadow-[0_12px_32px_-8px_rgb(0_0_0/0.28)]",
          className,
        )}
        {...props}
      />
    </M.Portal>
  );
}

export function MenuItem({ className, danger, ...props }: ComponentProps<typeof M.Item> & { danger?: boolean }) {
  return (
    <M.Item
      className={cn(
        "flex cursor-pointer select-none items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none " +
          "data-[disabled]:pointer-events-none data-[disabled]:opacity-45 data-[highlighted]:bg-panel-2 [&_svg]:size-4 [&_svg]:text-muted",
        danger && "text-err [&_svg]:text-err",
        className,
      )}
      {...props}
    />
  );
}

export const MenuSeparator = () => <M.Separator className="my-1 h-px bg-line" />;
export const MenuLabel = ({ children }: { children: ReactNode }) => (
  <M.Label className="px-2 py-1 text-xs text-muted">{children}</M.Label>
);
export const MenuSub = M.Sub;
export function MenuSubTrigger({ className, ...props }: ComponentProps<typeof M.SubTrigger>) {
  return (
    <M.SubTrigger
      className={cn(
        "flex cursor-pointer select-none items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none data-[highlighted]:bg-panel-2 data-[state=open]:bg-panel-2 [&_svg]:size-4 [&_svg]:text-muted",
        className,
      )}
      {...props}
    />
  );
}
export function MenuSubContent({ className, ...props }: ComponentProps<typeof M.SubContent>) {
  return (
    <M.Portal>
      <M.SubContent
        sideOffset={4}
        className={cn("anim-fade z-50 min-w-36 rounded-lg border border-line bg-panel p-1 shadow-[0_12px_32px_-8px_rgb(0_0_0/0.28)]", className)}
        {...props}
      />
    </M.Portal>
  );
}
