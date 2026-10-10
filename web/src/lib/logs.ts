// 实时日志：整个页面共用一条 SSE 连接（概览和日志页都用），断了 3 秒后续上。
// 每个分区（播放、任务、系统）各留最多 MAX 条：抓取刷屏时不会把播放日志挤掉

import { useSyncExternalStore } from "react";
import type { LogArea, LogItem } from "./api";

const MAX = 3000;

interface Snapshot {
  items: LogItem[];
  connected: boolean;
}

let items: LogItem[] = [];
let connected = false;
let snapshot: Snapshot = { items, connected };
let source: EventSource | null = null;
let retry: ReturnType<typeof setTimeout> | null = null;
let scheduled = false;
let instance = "";
let cursor = 0;
const listeners = new Set<() => void>();

export const areaOf = (l: LogItem): LogArea => l.area ?? "system";

/** 每个分区只留最新的 MAX 条（从后往前数），顺序不变。 */
function trim() {
  const counts: Record<string, number> = {};
  let over = false;
  for (const l of items) if ((counts[areaOf(l)] = (counts[areaOf(l)] ?? 0) + 1) > MAX) over = true;
  if (!over) return;
  const kept: Record<string, number> = {};
  const out: LogItem[] = [];
  for (let i = items.length - 1; i >= 0; i--) {
    const a = areaOf(items[i]);
    if ((kept[a] = (kept[a] ?? 0) + 1) <= MAX) out.push(items[i]);
  }
  items = out.reverse();
}

function emit() {
  // 日志多的时候（DEBUG）合并到下一帧再通知，避免每条都重渲染
  if (scheduled) return;
  scheduled = true;
  const flush = () => {
    scheduled = false;
    trim();
    snapshot = { items: items.slice(), connected };
    listeners.forEach(l => l());
  };
  if (document.hidden) setTimeout(flush, 500);
  else requestAnimationFrame(flush);
}

function connect() {
  retry = null;
  const after = cursor;
  const es = new EventSource(`/api/logs/stream?after=${after}&instance=${instance}`);
  source = es;
  es.onopen = () => {
    connected = true;
    emit();
  };
  es.onmessage = ev => {
    const item = JSON.parse(ev.data) as LogItem;
    if (item.id <= cursor) return;
    cursor = item.id;
    items.push(item);
    emit();
  };
  es.addEventListener("reset", ev => {
    instance = JSON.parse((ev as MessageEvent).data).instance;
    cursor = 0;
    items = [];
    emit();
  });
  es.addEventListener("gap", () => {
    items.push({ id: -Date.now(), ts: Date.now()/1000, level: "WARNING", name: "logs", area: "system", msg: "部分日志已超出缓冲，详情请查看服务日志文件。" });
    emit();
  });
  es.onerror = () => {
    connected = false;
    es.close();
    source = null;
    emit();
    if (listeners.size) retry = setTimeout(connect, 3000);
  };
}

export function clearLogs() {
  items = [];
  emit();
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  if (!source && !retry) connect();
  return () => {
    listeners.delete(cb);
    if (listeners.size) return;
    // 没人看了（比如回到登录页）就断开，不在后台反复重连
    source?.close();
    source = null;
    if (retry) clearTimeout(retry);
    retry = null;
    connected = false;
  };
}

export function useLogs(): Snapshot {
  return useSyncExternalStore(subscribe, () => snapshot);
}

export const LEVELS: Record<string, number> = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
