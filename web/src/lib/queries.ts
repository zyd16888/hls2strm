// 数据查询：react-query 管缓存和轮询；页面隐藏时不轮询

import { keepPreviousData, QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type FacetField, type FacetItem, type HealthResponse, type Job, type Library, type Meta, type SiteMeta, type Status, type Subscription } from "./api";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: true, staleTime: 1000 },
  },
});

const POLL = 3000;

export function useStatus() {
  return useQuery({ queryKey: ["status"], queryFn: ({ signal }) => api.get<Status>("/api/status", signal), refetchInterval: q => q.state.data?.engine.running.length ? POLL : 10000 });
}

export function useMeta() {
  const q = useQuery({ queryKey: ["meta"], queryFn: ({ signal }) => api.get<Meta>("/api/meta", signal), staleTime: Infinity });
  const sites = q.data?.sites ?? {};
  const site = (name: string): SiteMeta =>
    sites[name] ?? { label: name, presets: [], sorts: {}, default_sort: "", hint: "", direct: true, ip_bound: false, lines: [] };
  return { sites, site, label: (name: string | null | undefined) => (name ? site(name).label || name : "") };
}

export function useLibraries(poll = false) {
  return useQuery({
    queryKey: ["libraries"],
    queryFn: ({ signal }) => api.get<Library[]>("/api/libraries", signal),
    refetchInterval: poll ? POLL : false,
  });
}

export function useSubscriptions(poll = false) {
  return useQuery({
    queryKey: ["subscriptions"],
    queryFn: ({ signal }) => api.get<Subscription[]>("/api/subscriptions", signal),
    refetchInterval: poll ? POLL : false,
  });
}

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => api.get<HealthResponse>("/api/health", signal),
    refetchInterval: q => (q.state.data?.checking ? 3000 : 30000),
  });
}

export function useJobs() {
  return useQuery({ queryKey: ["jobs"], queryFn: ({ signal }) => api.get<Job[]>("/api/jobs?limit=100", signal), refetchInterval: POLL });
}

export function useFacet(field: FacetField, q = "", enabled = true) {
  return useQuery({
    queryKey: ["facet", field, q],
    queryFn: ({ signal }) => api.get<FacetItem[]>(`/api/facets?field=${field}&limit=${q ? 50 : 300}&q=${encodeURIComponent(q)}`, signal),
    enabled,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
}

/** 执行一个操作：成功提示、失败弹错误；完成后刷新相关数据。返回结果，失败返回 undefined。 */
export function useRun() {
  const qc = useQueryClient();
  return async function run<T>(
    fn: () => Promise<T>,
    opts: { success?: string | ((r: T) => string); invalidate?: unknown[][] } = {},
  ): Promise<T | undefined> {
    try {
      const r = await fn();
      const msg = typeof opts.success === "function" ? opts.success(r) : opts.success;
      if (msg) toast.success(msg);
      return r;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
      return undefined;
    } finally {
      for (const key of opts.invalidate ?? [["status"]]) qc.invalidateQueries({ queryKey: key });
    }
  };
}
