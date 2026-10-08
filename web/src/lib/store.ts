// 极简全局状态：试播、影片详情这类在任何页面都能打开的东西

import { useSyncExternalStore } from "react";

export function createStore<T>(initial: T) {
  let state = initial;
  const listeners = new Set<() => void>();
  const subscribe = (cb: () => void) => {
    listeners.add(cb);
    return () => listeners.delete(cb);
  };
  return {
    get: () => state,
    set(next: T | ((prev: T) => T)) {
      state = typeof next === "function" ? (next as (prev: T) => T)(state) : next;
      listeners.forEach(l => l());
    },
    use: () => useSyncExternalStore(subscribe, () => state),
  };
}
