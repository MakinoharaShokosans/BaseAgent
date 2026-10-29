/**
 * 自绘语义图标：24 网格、1.6 描边、圆角端点。通用操作仍用 lucide，这里只放表达"状态与角色"的图标。
 * 规范见 docs/10_frontend/03_visual_design.md §8。装饰性图标一律 aria-hidden。
 */
import { motion } from 'motion/react';
import type { SVGProps } from 'react';

type GlyphProps = { size?: number; className?: string };

function Svg({ size = 16, className, children, ...rest }: GlyphProps & SVGProps<SVGSVGElement>) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
      {...rest}
    >
      {children}
    </svg>
  );
}

/** 运行中：一段弧绕圆旋转 */
export function OrbitGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <circle cx="12" cy="12" r="8" opacity="0.22" />
      <motion.path
        d="M12 4a8 8 0 0 1 8 8"
        style={{ originX: '12px', originY: '12px' }}
        animate={{ rotate: 360 }}
        transition={{ repeat: Infinity, duration: 1.1, ease: 'linear' }}
      />
      <circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none" />
    </Svg>
  );
}

/** 成功：对勾描边绘制 */
export function CheckDraw({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <motion.circle
        cx="12"
        cy="12"
        r="8"
        initial={{ pathLength: 0, opacity: 0.4 }}
        animate={{ pathLength: 1, opacity: 1 }}
        transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
      />
      <motion.path
        d="m8.5 12.2 2.4 2.4 4.6-5"
        initial={{ pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={{ duration: 0.24, delay: 0.18, ease: [0.22, 1, 0.36, 1] }}
      />
    </Svg>
  );
}

export function CrossGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <circle cx="12" cy="12" r="8" />
      <path d="m9.5 9.5 5 5m0-5-5 5" />
    </Svg>
  );
}

/** 规划：四角星芒 */
export function PlanGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <path d="M12 3.5c.6 3.9 2.6 5.9 6.5 6.5-3.9.6-5.9 2.6-6.5 6.5-.6-3.9-2.6-5.9-6.5-6.5 3.9-.6 5.9-2.6 6.5-6.5Z" />
      <path d="M18.5 15.5c.25 1.6 1 2.35 2.5 2.5-1.5.25-2.25 1-2.5 2.5-.25-1.5-1-2.25-2.5-2.5 1.5-.15 2.25-.9 2.5-2.5Z" />
    </Svg>
  );
}

export function TerminalGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <rect x="3.5" y="5" width="17" height="14" rx="3" />
      <path d="m7.5 10 2.5 2-2.5 2M12.5 14.5h4" />
    </Svg>
  );
}

export function ToolGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <path d="M14.5 5.5a4 4 0 0 0-5 5L4.8 15.2a1.9 1.9 0 0 0 2.7 2.7l4.7-4.7a4 4 0 0 0 5-5l-2.4 2.4-2.3-.4-.4-2.3 2.4-2.4Z" />
    </Svg>
  );
}

/** 子智能体与研究：三个节点的连线 */
export function AgentGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <circle cx="12" cy="6" r="2.4" />
      <circle cx="6" cy="17.5" r="2.4" />
      <circle cx="18" cy="17.5" r="2.4" />
      <path d="M10.8 8.1 7.2 15.4M13.2 8.1l3.6 7.3M8.4 17.5h7.2" />
    </Svg>
  );
}

export function GlobeGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17M12 3.5c2.4 2.4 3.5 5.2 3.5 8.5s-1.1 6.1-3.5 8.5c-2.4-2.4-3.5-5.2-3.5-8.5S9.6 5.9 12 3.5Z" />
    </Svg>
  );
}

/** 守卫拦截：盾牌里一道横杠 */
export function GuardGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <path d="M12 3.5c2.5 1.6 5 2.4 7.5 2.5v5.6c0 4.6-3 7.8-7.5 10.4-4.5-2.6-7.5-5.8-7.5-10.4V6c2.5-.1 5-.9 7.5-2.5Z" />
      <path d="M9 12.5h6" />
    </Svg>
  );
}

export function ShieldCheckGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <path d="M12 3.5c2.5 1.6 5 2.4 7.5 2.5v5.6c0 4.6-3 7.8-7.5 10.4-4.5-2.6-7.5-5.8-7.5-10.4V6c2.5-.1 5-.9 7.5-2.5Z" />
      <path d="m9 12.3 2.2 2.2 4-4.3" />
    </Svg>
  );
}

/** 等待审批：举起的手 */
export function HandGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <path d="M8 12.5V6.8a1.4 1.4 0 0 1 2.8 0v4.7M10.8 11V5.4a1.4 1.4 0 0 1 2.8 0V11M13.6 11V6.6a1.4 1.4 0 0 1 2.8 0v6.2" />
      <path d="M16.4 10.2a1.4 1.4 0 0 1 2.8 0v3.6a7 7 0 0 1-7 7h-.6a6 6 0 0 1-4.6-2.2l-2.6-3.2a1.5 1.5 0 0 1 2.3-1.9L8 14.2" />
    </Svg>
  );
}

/** 记忆：三层叠片 */
export function MemoryGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <path d="m12 4 8 4-8 4-8-4 8-4Z" />
      <path d="m4 12 8 4 8-4" opacity="0.7" />
      <path d="m4 16 8 4 8-4" opacity="0.4" />
    </Svg>
  );
}

export function GaugeGlyph({ size, className }: GlyphProps) {
  return (
    <Svg size={size} className={className}>
      <path d="M4.5 16a8 8 0 1 1 15 0" />
      <path d="m12 13 3.5-4" />
      <circle cx="12" cy="13.5" r="1.2" fill="currentColor" stroke="none" />
    </Svg>
  );
}

/** 文件图标：按扩展名着色 */
const FILE_TINT: [RegExp, string][] = [
  [/\.(tsx?|jsx?|mjs|cjs)$/i, 'text-[#6b8ba8]'],
  [/\.(py|rb|go|rs|java|kt|c|cc|cpp|h|hpp|swift)$/i, 'text-accent-2'],
  [/\.(json|toml|ya?ml|ini|env.*|lock)$/i, 'text-warn'],
  [/\.(md|mdx|txt|rst)$/i, 'text-ok'],
  [/\.(css|scss|html|svg)$/i, 'text-accent'],
  [/\.(png|jpe?g|gif|webp|ico)$/i, 'text-bad'],
];

export function FileGlyph({ name, size = 15 }: { name: string; size?: number }) {
  const tint = FILE_TINT.find(([re]) => re.test(name))?.[1] ?? 'text-mute';
  return (
    <Svg size={size} className={tint}>
      <path d="M6.5 3.5h7l4 4v11.5a1.5 1.5 0 0 1-1.5 1.5h-9.5A1.5 1.5 0 0 1 5 19V5a1.5 1.5 0 0 1 1.5-1.5Z" />
      <path d="M13.5 3.5V7.5h4" />
    </Svg>
  );
}

export function FolderGlyph({ open, size = 15 }: { open: boolean; size?: number }) {
  return (
    <Svg size={size} className={open ? 'text-ink' : 'text-mute'}>
      {open ? (
        <path d="M3.5 7.5V6A1.5 1.5 0 0 1 5 4.5h4l2 2h7.5A1.5 1.5 0 0 1 20 8v1.5M3.8 19.5h14.4l2.3-8.5H6.1l-2.3 8.5Z" />
      ) : (
        <path d="M3.5 6A1.5 1.5 0 0 1 5 4.5h4l2 2h8A1.5 1.5 0 0 1 20.5 8v10a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18V6Z" />
      )}
    </Svg>
  );
}
