// 实时日志：整个页面共用一条 SSE 连接（概览和日志页都用），断了 3 秒后续上

import { useSyncExternalStore } from "react";
import type { LogItem } from "./api";

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
const listeners = new Set<() => void>();

function emit() {
  // 日志多的时候（DEBUG）合并到下一帧再通知，避免每条都重渲染
  if (scheduled) return;
  scheduled = true;
  const flush = () => {
    scheduled = false;
    snapshot = { items: items.slice(), connected };
    listeners.forEach(l => l());
  };
  if (document.hidden) setTimeout(flush, 500);
  else requestAnimationFrame(flush);
}

function connect() {
  retry = null;
  const after = items.length ? items[items.length - 1].id : 0;
  const es = new EventSource(`/api/logs/stream?after=${after}`);
  source = es;
  es.onopen = () => {
    connected = true;
    emit();
  };
  es.onmessage = ev => {
    const item = JSON.parse(ev.data) as LogItem;
    if (items.length && item.id <= items[items.length - 1].id) return;
    items.push(item);
    if (items.length > MAX) items.splice(0, items.length - MAX);
    emit();
  };
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
