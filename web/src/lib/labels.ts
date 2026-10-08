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
  crawl: "列表抓取",
  incremental: "增量",
  videos: "指定影片",
  probe: "补源",
  verify: "核对输出",
  cover: "补封面",
};
export const kindName = (k: string) => KIND_NAMES[k] || k;

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
  other: "其他来源",
  invalid: "无效",
};

export const SUBTITLES: Record<string, string> = { zh: "中文字幕", en: "英文字幕", "": "无字幕" };
export const subtitleTag = (code: string) => (code === "zh" ? "中字" : code === "en" ? "英字" : "");

export const PLAY_MODES: Record<string, string> = {
  redirect: "302 跳转（ffmpeg 类客户端自动中转）",
  proxy: "全部中转",
  direct: "直写 CDN 地址（仅调试）",
};

export const PROBE_STATUS: Record<string, [string, Tone]> = {
  found: ["找到", "ok"],
  none: ["没有", "neutral"],
  failed: ["失败", "err"],
  timeout: ["超时", "warn"],
};
