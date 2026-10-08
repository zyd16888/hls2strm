import { Command } from "cmdk";
import { Check, ChevronDown } from "lucide-react";
import { useEffect, useState } from "react";
import type { FacetField, FacetItem } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { useFacet } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/overlay";

// 候选的显示名（女优、分类在地址栏里存的是 id / slug）：见过的都记下来
const names = new Map<string, string>();
const remember = (field: FacetField, items: { item: string; name: string }[]) =>
  items.forEach(i => names.set(`${field}:${i.item}`, i.name));

export function facetName(field: FacetField, id: string) {
  return names.get(`${field}:${id}`) ?? id;
}

export function rememberFacet(field: FacetField, item: string, name: string) {
  names.set(`${field}:${item}`, name);
}

/** 预取常见候选，让地址栏里的筛选条件能显示名字。 */
export function useFacetNames(field: FacetField, enabled: boolean) {
  const { data } = useFacet(field, "", enabled);
  if (data) remember(field, data);
}

export function FacetSelect({
  field,
  label,
  value,
  onChange,
  className,
}: {
  field: FacetField;
  label: string;
  value: string[];
  onChange: (v: string[]) => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);
  const { data, isFetching } = useFacet(field, debounced, open);
  const items: FacetItem[] = data ?? [];
  useEffect(() => {
    if (data) remember(field, data);
  }, [data, field]);

  const toggle = (id: string) => onChange(value.includes(id) ? value.filter(x => x !== id) : [...value, id]);
  const chosen = value.filter(id => !items.some(i => i.item === id));

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        className={cn(
          "flex h-8 min-w-0 items-center gap-1.5 rounded-md border border-line bg-panel px-2.5 text-sm hover:bg-panel-2 data-[state=open]:border-accent/60",
          className,
        )}
      >
        <span className={cn("truncate", !value.length && "text-muted")}>
          {value.length ? value.map(id => facetName(field, id)).join("、") : `${label}：不限`}
        </span>
        {value.length > 1 && <span className="rounded bg-accent-soft px-1 text-xs text-accent">{value.length}</span>}
        <ChevronDown className="ml-auto size-3.5 shrink-0 text-muted" />
      </PopoverTrigger>
      <PopoverContent className="w-72 p-0">
        <Command shouldFilter={false} label={label}>
          <div className="border-b border-line p-2">
            <Command.Input
              value={q}
              onValueChange={setQ}
              placeholder={`搜索${label}`}
              className="h-8 w-full rounded-md border border-line bg-panel px-2.5 text-sm placeholder:text-muted focus:outline-none focus-visible:border-accent/60"
            />
          </div>
          <Command.List className="scroll-thin max-h-72 overflow-y-auto p-1">
            {chosen.map(id => (
              <Row key={id} label={facetName(field, id)} selected onSelect={() => toggle(id)} />
            ))}
            {items.map(i => (
              <Row key={i.item} label={i.name} sub={i.name !== i.item && field !== "models" ? i.item : ""} n={i.n} selected={value.includes(i.item)} onSelect={() => toggle(i.item)} />
            ))}
            <Command.Empty className="px-2 py-6 text-center text-[13px] text-muted">{isFetching ? "加载中…" : "没有匹配的"}</Command.Empty>
          </Command.List>
          {value.length > 0 && (
            <div className="border-t border-line p-1.5">
              <button type="button" className="w-full rounded-md px-2 py-1 text-left text-[13px] text-muted hover:bg-panel-2 hover:text-ink" onClick={() => onChange([])}>
                清除{label}条件
              </button>
            </div>
          )}
        </Command>
      </PopoverContent>
    </Popover>
  );
}

function Row({ label, sub, n, selected, onSelect }: { label: string; sub?: string; n?: number; selected: boolean; onSelect: () => void }) {
  return (
    <Command.Item
      value={label + (sub ?? "")}
      onSelect={onSelect}
      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm data-[selected=true]:bg-panel-2"
    >
      <span className={cn("flex size-4 items-center justify-center rounded border border-line", selected && "border-accent bg-accent text-accent-ink")}>
        {selected && <Check className="size-3" />}
      </span>
      <span className="min-w-0 flex-1 truncate">
        {label}
        {sub && <span className="ml-1.5 text-xs text-muted">{sub}</span>}
      </span>
      {n != null && <span className="text-xs text-muted">{fmtNum(n)}</span>}
    </Command.Item>
  );
}
