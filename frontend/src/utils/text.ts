import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/* eslint-disable no-control-regex */
const ANSI =
  /\u001b\[[0-?]*[ -/]*[@-~]|\u001b\][^\u0007]*\u0007|[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;
/* eslint-enable no-control-regex */

/** 终端控制字符与 ANSI 转义序列：显示前剥离，避免界面被污染。保留换行与制表符。 */
export function stripControl(text: string): string {
  return text.replace(ANSI, '');
}

export function safeHref(href: string | undefined): string | undefined {
  if (!href) return undefined;
  try {
    const url = new URL(href, 'https://placeholder.invalid');
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) && /^(https?:|mailto:)/i.test(href)
      ? href
      : undefined;
  } catch {
    return undefined;
  }
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/**
 * Token 估算，与后端 backend/src/agent_runtime/llm/tokens.py 的 estimate_tokens 同一规则：
 * 中日韩字符一字一 token，其余每 3 字符一 token（宁高勿低）。两边改动要同步。
 */
const WIDE = /[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]/g;

export function estimateTokens(text: string): number {
  if (!text) return 0;
  const wide = text.match(WIDE)?.length ?? 0;
  return wide + Math.ceil((text.length - wide) / 3);
}

export function relativePath(path: string, root: string): string {
  return path.startsWith(root + '/') ? path.slice(root.length + 1) : path;
}

/** 越级命令里的高危模式：命中时审批卡片用强提示并延迟启用批准。 */
export const DANGEROUS_PATTERNS: readonly { pattern: RegExp; label: string }[] = [
  { pattern: /\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r/i, label: '递归强制删除' },
  { pattern: /\bsudo\b/, label: '提权（sudo）' },
  { pattern: /git\s+push\b.*(--force|-f\b)/, label: '强制推送' },
  { pattern: /\|\s*(ba|z)?sh\b/, label: '把下载内容直接交给 shell 执行' },
  { pattern: /\bchmod\s+-R\b|\bchown\s+-R\b/, label: '递归修改权限或属主' },
];

export function dangerLabels(command: string): string[] {
  return DANGEROUS_PATTERNS.filter((d) => d.pattern.test(command)).map((d) => d.label);
}
