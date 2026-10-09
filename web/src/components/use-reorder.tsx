import { GripVertical } from "lucide-react";
import { useRef, useState } from "react";
import { Button } from "./ui/button";

/** 指针拖动在松手时提交，触屏和键盘共用同一份排序草稿。 */
export function useReorder(items: string[], onChange: (items: string[]) => void) {
  const root = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ from: string; to: string } | null>(null);
  const target = useRef<string | null>(null);
  const move = (from: string, to: string) => {
    const a = items.indexOf(from), b = items.indexOf(to);
    if (a < 0 || b < 0 || a === b) return;
    const next = [...items];
    next.splice(a, 1);
    next.splice(b, 0, from);
    onChange(next);
  };
  const cancel = () => { target.current = null; setDrag(null); };
  return {
    root,
    row: (id: string) => ({
      "data-sort-key": id,
      className: drag?.to === id ? "bg-accent-soft outline outline-accent -outline-offset-1" : drag?.from === id ? "opacity-50" : "",
    }),
    handle: (id: string, label: string) => (
      <Button
        size="icon-sm" variant="ghost" className="touch-none cursor-grab active:cursor-grabbing"
        aria-label={`拖动排序：${label}，也可用上下方向键移动`} title="拖动排序；也可用上下方向键移动"
        onPointerDown={e => {
          if (e.button !== 0) return;
          e.currentTarget.setPointerCapture(e.pointerId);
          target.current = id;
          setDrag({ from: id, to: id });
        }}
        onPointerMove={e => {
          if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
          const row = document.elementFromPoint(e.clientX, e.clientY)?.closest<HTMLElement>("[data-sort-key]");
          const to = row && root.current?.contains(row) ? row.dataset.sortKey! : null;
          target.current = to;
          setDrag(to ? { from: id, to } : null);
        }}
        onPointerUp={e => {
          if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
          if (target.current) move(id, target.current);
          e.currentTarget.releasePointerCapture(e.pointerId);
          cancel();
        }}
        onPointerCancel={cancel} onLostPointerCapture={cancel}
        onKeyDown={e => {
          if (e.key === "Escape") { cancel(); return; }
          if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
          e.preventDefault();
          const to = items[items.indexOf(id) + (e.key === "ArrowUp" ? -1 : 1)];
          if (to) move(id, to);
        }}
      ><GripVertical /></Button>
    ),
  };
}
