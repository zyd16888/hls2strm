// 几个页面都会用到的操作

import { ask } from "@/components/confirm";
import { api, type Library } from "./api";
import type { useRun } from "./queries";

type Run = ReturnType<typeof useRun>;

export async function verifyLibrary(run: Run, lib: Library | null) {
  const name = lib ? `「${lib.name}」` : "全部库";
  const ok = await ask(
    `核对${name}`,
    <>
      <p>检查数据库里的每条输出在磁盘上还在不在（strm、nfo、封面），缺的补回。</p>
      <p>strm 和 nfo 在本地重写，封面先从别的库硬链接，没有再下载。</p>
      <p>外部整理库按内容找文件（外部工具改名、加后缀也认得出），找到只更新路径；两边都找不到才写回收件目录；外部整理目录不存在或是空的不补。</p>
    </>,
    { confirmText: "开始核对" },
  );
  if (!ok) return;
  await run(() => api.post<{ id: number }>("/api/jobs", { kind: "verify", library_id: lib?.id ?? null, repair: true, covers: true }), {
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
