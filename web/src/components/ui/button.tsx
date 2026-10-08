import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";
import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-md font-medium transition-colors " +
    "disabled:pointer-events-none disabled:opacity-45 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-ink hover:bg-accent/88 active:bg-accent/80",
        outline: "border border-line bg-panel text-ink hover:bg-panel-2",
        ghost: "text-ink hover:bg-panel-2",
        danger: "border border-line bg-panel text-err hover:bg-err-soft",
        quiet: "text-muted hover:bg-panel-2 hover:text-ink",
      },
      size: {
        sm: "h-7 px-2.5 text-[13px] [&_svg]:size-3.5",
        md: "h-8 px-3 text-sm",
        icon: "size-8",
        "icon-sm": "size-7 [&_svg]:size-3.5",
      },
    },
    defaultVariants: { variant: "outline", size: "md" },
  },
);

export function Button({
  className,
  variant,
  size,
  asChild = false,
  type = "button",
  ...props
}: ComponentProps<"button"> & VariantProps<typeof buttonVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "button";
  return <Comp type={asChild ? undefined : type} className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
