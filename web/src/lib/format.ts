export function fmtNum(n: number | null | undefined): string {
  return n == null ? "-" : Number(n).toLocaleString("zh-CN");
}

/** '512 KB' / '3.2 MB' / '1.5 GB'。 */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "-";
  if (n < 1024 ** 2) return `${Math.max(1, Math.round(n / 1024))} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(1)} GB`;
}

export function pct(a: number | null | undefined, b: number | null | undefined): string {
  return b ? Math.round(((a || 0) * 100) / b) + "%" : "-";
}

const p2 = (x: number) => String(x).padStart(2, "0");

/** cron 时间按订阅时区显示，不随浏览器时区改变。 */
export function fmtScheduledTime(ts: number, timezone: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(ts * 1000));
}

/** 'MM-DD HH:MM:SS'；跨年时带年份。 */
export function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const year = d.getFullYear() !== new Date().getFullYear() ? `${d.getFullYear()}-` : "";
  return `${year}${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}

export function fmtClock(ts: number | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}

/** 时长：'45 秒' / '3 分 12 秒' / '2 小时 5 分' / '1 天 3 小时'。 */
export function fmtDur(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec)) return "-";
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时 ${Math.floor((sec % 3600) / 60)} 分`;
  return `${Math.floor(sec / 86400)} 天 ${Math.floor((sec % 86400) / 3600)} 小时`;
}

/** 片长：'2:30:18'。 */
export function fmtClockDur(sec: number | null | undefined): string {
  if (!sec) return "";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? `${h}:${p2(m)}:${p2(s)}` : `${m}:${p2(s)}`;
}

/** 相对现在：'3 分钟前'、'2 天前'。 */
export function fmtAgo(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "-";
  const d = Math.max(0, now - ts);
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}
