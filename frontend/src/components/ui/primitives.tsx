import { AnimatePresence, motion } from 'motion/react';
import { ChevronDown, ChevronRight, X } from 'lucide-react';
import { useEffect, useRef, type ComponentProps, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useUiStore } from '../../stores/useUiStore';
import { cn } from '../../utils/text';

/** 动效令牌，见 docs/10_frontend/03_visual_design.md §8 */
export const EASE_OUT = [0.22, 1, 0.36, 1] as const;
export const SPRING = { type: 'spring', stiffness: 380, damping: 34 } as const;

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';

const VARIANTS: Record<Variant, string> = {
  primary:
    'bg-ink text-canvas shadow-card hover:bg-ink/85 disabled:bg-ink/15 disabled:text-mute disabled:shadow-none',
  secondary: 'bg-ink/[0.06] text-ink hover:bg-ink/[0.1] disabled:text-mute',
  danger: 'bg-bad text-white shadow-card hover:brightness-95 disabled:opacity-40',
  ghost: 'text-mute hover:bg-ink/[0.06] hover:text-ink disabled:text-mute/40',
};

export function Button({
  variant = 'secondary',
  className,
  ...props
}: ComponentProps<'button'> & { variant?: Variant }) {
  return (
    <button
      className={cn(
        'inline-flex select-none items-center justify-center gap-1.5 rounded-xl px-3.5 py-1.5 text-[13px] font-semibold',
        'transition-[background-color,color,border-color,box-shadow,transform] duration-150 ease-out active:scale-[0.97]',
        'disabled:cursor-not-allowed disabled:active:scale-100',
        VARIANTS[variant],
        className,
      )}
      {...props}
    />
  );
}

export function IconButton({
  className,
  active,
  label,
  ...props
}: ComponentProps<'button'> & { label: string; active?: boolean }) {
  return (
    <button
      aria-label={label}
      title={label}
      className={cn(
        'inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-mute transition-colors duration-150',
        'hover:bg-ink/[0.07] hover:text-ink active:scale-95',
        active && 'bg-ink/[0.07] text-ink',
        className,
      )}
      {...props}
    />
  );
}

const TONES = {
  slate: 'bg-ink/[0.06] text-mute',
  green: 'bg-ok/12 text-ok',
  amber: 'bg-warn/12 text-warn',
  red: 'bg-bad/12 text-bad',
  blue: 'bg-accent/12 text-accent',
  violet: 'bg-accent-2/12 text-accent-2',
} as const;

export type Tone = keyof typeof TONES;

export function Badge({
  tone = 'slate',
  children,
  className,
}: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold',
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/** 状态胶囊：图标 + 文字；`live` 时有一道流光扫过 */
export function StatusPill({
  tone,
  icon,
  live,
  children,
  className,
}: {
  tone: Tone;
  icon?: ReactNode;
  live?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        'relative inline-flex items-center gap-1.5 overflow-hidden rounded-full py-1 pl-2 pr-2.5 text-xs font-semibold',
        TONES[tone],
        className,
      )}
    >
      {live && (
        <span
          aria-hidden
          className="pointer-events-none absolute inset-y-0 left-0 w-1/2 animate-sheen bg-gradient-to-r from-transparent via-white/40 to-transparent dark:via-white/15"
        />
      )}
      {icon}
      {children}
    </span>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded-md bg-ink/[0.06] px-1 font-mono text-[10.5px] font-medium text-mute">
      {children}
    </kbd>
  );
}

/** 高度自动动画的折叠区 */
export function Reveal({ open, children }: { open: boolean; children: ReactNode }) {
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          key="reveal"
          // 只在动画期间裁切；展开后放开，里面的下拉弹层才不会被裁掉
          initial={{ height: 0, opacity: 0, overflow: 'hidden' }}
          animate={{ height: 'auto', opacity: 1, transitionEnd: { overflow: 'visible' } }}
          exit={{ height: 0, opacity: 0, overflow: 'hidden' }}
          transition={{ duration: 0.22, ease: EASE_OUT }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function Chevron({ open, size = 14 }: { open: boolean; size?: number }) {
  return (
    <ChevronRight
      size={size}
      aria-hidden
      className={cn(
        'shrink-0 text-mute transition-transform duration-200 ease-out',
        open && 'rotate-90',
      )}
    />
  );
}

/** 右侧栏的可折叠卡片：标题行点击折叠，折叠状态记在本地偏好 */
export function Card({
  id,
  title,
  icon,
  aside,
  children,
  className,
}: {
  id: string;
  title: string;
  icon?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const open = useUiStore((s) => !s.collapsedCards.includes(id));
  const toggle = useUiStore((s) => s.toggleCard);
  return (
    <section aria-label={title} className={cn('soft-card px-4 py-3.5', className)}>
      <header className="flex items-center gap-2">
        <button
          className="flex min-w-0 flex-1 items-center gap-2 text-left text-[14px] font-semibold"
          aria-expanded={open}
          onClick={() => toggle(id)}
        >
          {icon && <span className="text-mute">{icon}</span>}
          <span className="truncate">{title}</span>
          <ChevronDown
            size={14}
            aria-hidden
            className={cn(
              'shrink-0 text-mute transition-transform duration-200',
              !open && '-rotate-90',
            )}
          />
        </button>
        {aside}
      </header>
      <Reveal open={open}>
        <div className="pt-3">{children}</div>
      </Reveal>
    </section>
  );
}

/**
 * 遮罩 + 居中对话框的外壳：Esc 关闭，焦点落在对话框。
 * 渲染到 body：祖先上的 backdrop-filter / transform 会成为 fixed 定位的包含块，把对话框困在侧栏里。
 */
export function Overlay({
  label,
  onClose,
  children,
  className,
}: {
  label: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    ref.current?.focus();
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <motion.div
        aria-hidden
        className="absolute inset-0 bg-ink/10 backdrop-blur-md dark:bg-black/40"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.2 }}
        onClick={onClose}
      />
      <motion.div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        initial={{ opacity: 0, scale: 0.96, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={{ duration: 0.26, ease: EASE_OUT }}
        className={cn(
          'relative max-h-[90vh] w-full overflow-auto rounded-[24px] bg-raised shadow-float outline-none no-scrollbar',
          className,
        )}
      >
        {children}
      </motion.div>
    </div>,
    document.body,
  );
}

export function Modal({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <Overlay label={title} onClose={onClose} className={cn('p-6', wide ? 'max-w-3xl' : 'max-w-md')}>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-[17px] font-semibold text-ink">{title}</h2>
        <IconButton label="关闭" onClick={onClose}>
          <X size={16} />
        </IconButton>
      </div>
      {children}
    </Overlay>
  );
}
