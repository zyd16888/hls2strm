// 几个页面都会用到的操作

import { ask } from "@/components/confirm";
import { defaultVerifyOptions, verifyParams, VerifyPrompt } from "@/components/verify-options";
import { api, type Library } from "./api";
import type { useRun } from "./queries";

type Run = ReturnType<typeof useRun>;

export async function verifyLibrary(run: Run, lib: Library | null) {
  const name = lib ? `「${lib.name}」` : "全部库";
  let options = { ...defaultVerifyOptions };
  const ok = await ask(
    `核对${name}`,
    <VerifyPrompt onChange={v => { options = v; }} />,
    { confirmText: "开始核对" },
  );
  if (!ok) return;
  await run(() => api.post<{ id: number }>("/api/jobs", { kind: "verify", library_id: lib?.id ?? null, ...verifyParams(options) }), {
    success: r => `已创建核对任务 #${r.id}`,
    invalidate: [["status"], ["jobs"]],
  });
}

export async function rewriteAll(run: Run, lib: Library | null = null) {
  const ok = await ask(
    lib ? `重写「${lib.name}」的输出` : "重写全部输出",
    "按当前设置重写 strm 和 nfo（不联网），路径变了会搬动文件。改了对外地址、播放模式、令牌或路径模板之后用它。",
    { confirmText: "开始重写" },
  );
  if (!ok) return;
  await run(() => api.post<{ id: number }>("/api/jobs", { kind: "rewrite", library_id: lib?.id ?? null }), {
    success: r => `已创建重写任务 #${r.id}`,
    invalidate: [["status"], ["jobs"]],
  });
}

export async function runSubscription(run: Run, sub: { id: number; name: string }, mode: "full" | "incremental") {
  if (
    mode === "full" &&
    !(await ask(
      `订阅「${sub.name}」跑一轮全量`,
      "翻完来源的全部页。全站约 3.9 万部时，列表约 30 分钟、详情约 11 小时；中途可以暂停或重启，会自动续跑。",
      { confirmText: "开始全量" },
    ))
  )
    return;
  await run(() => api.post<{ job_id: number }>(`/api/subscriptions/${sub.id}/run?mode=${mode}`), {
    success: r => `已创建任务 #${r.job_id}`,
    invalidate: [["status"], ["subscriptions"], ["jobs"]],
  });
}
