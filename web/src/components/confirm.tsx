// 确认对话框：confirm({...}) 返回用户选的那个按钮的值，取消返回 null

import type { ReactNode } from "react";
import { createStore } from "@/lib/store";
import { Button } from "./ui/button";
import { Dialog, DialogContent } from "./ui/overlay";

interface Choice {
  value: string;
  label: string;
  variant?: "primary" | "danger" | "outline";
}

interface Request {
  title: string;
  body?: ReactNode;
  choices: Choice[];
  resolve: (v: string | null) => void;
}

const store = createStore<Request | null>(null);

export function confirm(opts: { title: string; body?: ReactNode; choices?: Choice[]; confirmText?: string; danger?: boolean }) {
  const choices = opts.choices ?? [
    { value: "ok", label: opts.confirmText ?? "确定", variant: opts.danger ? "danger" : "primary" },
  ];
  return new Promise<string | null>(resolve => {
    store.get()?.resolve(null);
    store.set({ title: opts.title, body: opts.body, choices, resolve });
  });
}

/** 只要「确定 / 取消」时用：确定返回 true。 */
export async function ask(title: string, body?: ReactNode, opts: { confirmText?: string; danger?: boolean } = {}) {
  return (await confirm({ title, body, ...opts })) !== null;
}

export function ConfirmHost() {
  const req = store.use();
  const close = (v: string | null) => {
    req?.resolve(v);
    store.set(null);
  };
  return (
    <Dialog open={!!req} onOpenChange={open => !open && close(null)}>
      {req && (
        <DialogContent
          title={req.title}
          footer={
            <>
              <Button onClick={() => close(null)}>取消</Button>
              {req.choices.map((c, i) => (
                <Button key={c.value} variant={c.variant ?? "outline"} autoFocus={i === req.choices.length - 1} onClick={() => close(c.value)}>
                  {c.label}
                </Button>
              ))}
            </>
          }
        >
          {req.body && <div className="space-y-2 text-sm leading-relaxed">{req.body}</div>}
        </DialogContent>
      )}
    </Dialog>
  );
}
