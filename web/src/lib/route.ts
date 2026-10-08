// hash 路由：#/videos?site=jable。后端只在 / 提供页面，用 hash 不用改服务端；旧版的 #videos 也认得

import { useMemo, useSyncExternalStore } from "react";

export interface Route {
  page: string;
  params: URLSearchParams;
}

function parse(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [page, query = ""] = raw.split("?", 2);
  return { page: page || "overview", params: new URLSearchParams(query) };
}

function subscribe(cb: () => void) {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => location.hash);
  return useMemo(() => parse(hash), [hash]);
}

function build(page: string, params?: URLSearchParams | Record<string, string>): string {
  const p = params instanceof URLSearchParams ? params : new URLSearchParams(params);
  const q = p.toString();
  return `#/${page}${q ? "?" + q : ""}`;
}

/** 换页（进历史记录）。 */
export function navigate(page: string, params?: URLSearchParams | Record<string, string>) {
  location.hash = build(page, params);
}

/** 只改当前页的参数（筛选、翻页）：替换历史记录，不刷屏后退按钮。 */
export function replaceParams(params: URLSearchParams) {
  const url = build(parse(location.hash).page, params);
  if (url === location.hash) return;
  history.replaceState(null, "", url);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}
