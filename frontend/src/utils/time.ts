/** 相对时间：秒级时间戳 → “刚刚 / 3 分 / 2 小时 / 昨天 / 6-12”。 */
export function relativeTime(ts: number, now: number = Date.now() / 1000): string {
  const d = Math.max(0, now - ts);
  if (d < 45) return '刚刚';
  if (d < 3600) return `${Math.round(d / 60)} 分`;
  if (d < 86400) return `${Math.round(d / 3600)} 小时`;
  if (d < 172800) return '昨天';
  const date = new Date(ts * 1000);
  return `${date.getMonth() + 1}-${date.getDate()}`;
}

export function clock(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 毫秒 → “850ms / 3.2s / 2m05s” */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m${String(s).padStart(2, '0')}s`;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}
