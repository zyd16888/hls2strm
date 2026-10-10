// 界面上的中文名称

import type { Tone } from "@/components/ui/data";
import type { JobStatus, TaskStatus } from "./api";

export const KIND_NAMES: Record<string, string> = {
  list: "列表页",
  detail: "详情页",
  rewrite: "重写输出",
  purge: "删除库",
  reclassify: "重新归库",
  scan: "扫描",
  adopt: "纳管",
  prefix: "改前缀",
  revert: "回滚",
  locate: "同步位置",
  tidy: "清理残留",
  crawl: "列表抓取",
  incremental: "增量",
  videos: "指定影片",
  probe: "补源",
  verify: "核对输出",
  cover: "补封面",
  quality: "画质探测",
  prepare: "准备与分批入队",
  membership: "调整影片归属",
  library_add: "加入输出库",
  library_remove: "移出输出库",
};
export const kindName = (k: string) => KIND_NAMES[k] || k;

const QUALITY_SOURCES: Record<string, string> = { master: "主播放列表", embed: "播放页标注", estimate: "按码率估计", claimed: "站点标注" };
const qualityName = (h: number) => (h >= 2000 ? "4K" : `${h}p`);

/** 画质徽标的文字和说明：估计的、站点标注的前面加「约」；不知道返回 null */
export function qualityChip(row: { height: number | null; heights: string; quality_src: string }): { text: string; title: string } | null {
  if (!row.height) return null;
  const rough = row.quality_src === "estimate" || row.quality_src === "claimed";
  const all = (row.heights || String(row.height)).split(",").filter(Boolean).map(Number);
  return {
    text: (rough ? "约 " : "") + qualityName(row.height),
    title: `画质 ${all.map(qualityName).join(" / ")}（${QUALITY_SOURCES[row.quality_src] ?? "来源未知"}）`,
  };
}

export const JOB_STATUS: Record<JobStatus, [string, Tone]> = {
  running: ["运行中", "ok"],
  paused: ["已暂停", "warn"],
  done: ["已完成", "info"],
  cancelled: ["已取消", "neutral"],
};

export const TASK_STATUS: Record<TaskStatus, [string, Tone]> = {
  pending: ["待处理", "neutral"],
  running: ["运行中", "warn"],
  done: ["完成", "ok"],
  failed: ["失败", "err"],
  gone: ["下架", "neutral"],
  cancelled: ["已取消", "neutral"],
};

export const STRM_KINDS: Record<string, string> = {
  ours: "本服务格式",
  cdn: "CDN 直链",
  named: "文件名识别",
  version: "多画质版本",
  other: "其他来源",
  invalid: "无效",
};

export const VERSION_STYLES: Record<string, string> = {
  "": "不写",
  emby: "Emby 风格：目录名 - 720p.strm",
  suffix: "后缀风格：文件名-720p.strm（和 mdcng 一样）",
};

export const SUBTITLES: Record<string, string> = { zh: "中文字幕", en: "英文字幕", "": "无字幕" };
export const subtitleTag = (code: string) => (code === "zh" ? "中字" : code === "en" ? "英字" : "");

export const PLAY_MODES: Record<string, string> = {
  redirect: "可直连时 302，受限时原样中转",
  proxy: "全部原样中转（不转码）",
  direct: "直写 CDN 地址（仅调试）",
};

export const DOMAIN_MODES: Record<string, string> = {
  priority: "按顺序优先（失败再换）",
  round_robin: "轮询（依次分摊请求）",
  balanced: "负载均衡（处理中少的优先，再看响应速度）",
};

export const VARIANT_MODES: Record<string, string> = {
  highest: "只给最高一档（画质优先）",
  all: "全部给播放器，按网速自适应",
};

export const RESOLVE_MODES: Record<string, string> = {
  auto: "按挑源偏好（能直连给 CDN 地址，要中转给中转地址）",
  redirect: "优先直连，失败可回退中转",
  strict_redirect: "仅直连，禁止中转回退",
  proxy: "一律原样中转（不转码）",
};

export const PROBE_STATUS: Record<string, [string, Tone]> = {
  found: ["找到", "ok"],
  none: ["没有", "neutral"],
  failed: ["失败", "err"],
  timeout: ["超时", "warn"],
};
