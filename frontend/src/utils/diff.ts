/** 逐行差异：LCS，行数很大时退化为“整块删除 + 整块新增”。 */
export type DiffLine = { kind: 'same' | 'add' | 'del'; text: string };

const MAX_LINES = 600;

export function diffLines(before: string, after: string): DiffLine[] {
  const a = before === '' ? [] : before.split('\n');
  const b = after === '' ? [] : after.split('\n');
  if (a.length > MAX_LINES || b.length > MAX_LINES) {
    return [
      ...a.map((text) => ({ kind: 'del' as const, text })),
      ...b.map((text) => ({ kind: 'add' as const, text })),
    ];
  }
  const n = a.length;
  const m = b.length;
  const table: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      (table[i] as number[])[j] =
        a[i] === b[j]
          ? ((table[i + 1] as number[])[j + 1] as number) + 1
          : Math.max(
              (table[i + 1] as number[])[j] as number,
              (table[i] as number[])[j + 1] as number,
            );
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: 'same', text: a[i] as string });
      i++;
      j++;
    } else if (
      ((table[i + 1] as number[])[j] as number) >= ((table[i] as number[])[j + 1] as number)
    ) {
      out.push({ kind: 'del', text: a[i++] as string });
    } else {
      out.push({ kind: 'add', text: b[j++] as string });
    }
  }
  while (i < n) out.push({ kind: 'del', text: a[i++] as string });
  while (j < m) out.push({ kind: 'add', text: b[j++] as string });
  return out;
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  return {
    added: lines.filter((l) => l.kind === 'add').length,
    removed: lines.filter((l) => l.kind === 'del').length,
  };
}
