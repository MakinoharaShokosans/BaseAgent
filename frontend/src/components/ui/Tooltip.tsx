/**
 * 悬停气泡：自绘，替代浏览器原生 title。渲染到 body（z-tip，高于弹窗与下拉），
 * 悬停稍作停顿后出现、键盘聚焦立即出现，离开即消失；触屏设备不显示。
 */
import { AnimatePresence, motion } from 'motion/react';
import {
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
  type Ref,
} from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../../utils/text';
import { useAnchor, type Side } from './useAnchor';

const SHOW_DELAY_MS = 380;

/** 同一时刻只留一个气泡 */
let closeCurrent: (() => void) | null = null;

interface TipProps {
  label: ReactNode;
  /** 快捷键等次要信息，显示在标签右侧 */
  hint?: string;
  side?: Side;
  align?: 'start' | 'center' | 'end';
  disabled?: boolean;
  children: ReactElement<Record<string, unknown>>;
}

function canHover(): boolean {
  return typeof window.matchMedia !== 'function' || window.matchMedia('(hover: hover)').matches;
}

function setRef<T>(ref: Ref<T> | undefined, value: T | null): void {
  if (typeof ref === 'function') ref(value);
  else if (ref) (ref as { current: T | null }).current = value;
}

export function Tip({
  label,
  hint,
  side = 'bottom',
  align = 'center',
  disabled,
  children,
}: TipProps) {
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLElement | null>(null);
  const pop = useRef<HTMLDivElement | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const id = useId();
  const placement = useAnchor(open, anchor, pop, side, align);

  const hide = useCallback(() => {
    clearTimeout(timer.current);
    setOpen(false);
  }, []);

  const show = useCallback(
    (delay: number) => {
      clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        closeCurrent?.();
        closeCurrent = hide;
        setOpen(true);
      }, delay);
    },
    [hide],
  );

  useEffect(
    () => () => {
      clearTimeout(timer.current);
      if (closeCurrent === hide) closeCurrent = null;
    },
    [hide],
  );

  // 点击、滚动、按键都会让气泡失去意义
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && hide();
    window.addEventListener('keydown', onKey);
    window.addEventListener('scroll', hide, true);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', hide, true);
    };
  }, [open, hide]);

  if (!isValidElement(children) || disabled || !label) return children;
  const child = children as ReactElement<Record<string, unknown>>;
  const props = child.props;
  const childRef = props['ref'] as Ref<HTMLElement> | undefined; // React 19：ref 是普通属性
  const call = (name: string, e: unknown) =>
    (props[name] as ((e: unknown) => void) | undefined)?.(e);

  return (
    <>
      {cloneElement(child, {
        ref: (node: HTMLElement | null) => {
          anchor.current = node;
          setRef(childRef, node);
        },
        'aria-describedby': open ? id : props['aria-describedby'],
        onMouseEnter: (e: unknown) => {
          call('onMouseEnter', e);
          if (canHover()) show(SHOW_DELAY_MS);
        },
        onMouseLeave: (e: unknown) => {
          call('onMouseLeave', e);
          hide();
        },
        onFocus: (e: unknown) => {
          call('onFocus', e);
          if ((e as { target?: HTMLElement }).target?.matches?.(':focus-visible')) show(0);
        },
        onBlur: (e: unknown) => {
          call('onBlur', e);
          hide();
        },
        onPointerDown: (e: unknown) => {
          call('onPointerDown', e);
          hide();
        },
      })}
      {createPortal(
        <AnimatePresence>
          {open && (
            <motion.div
              ref={pop}
              id={id}
              role="tooltip"
              initial={{ opacity: 0, scale: 0.96 }}
              animate={{ opacity: placement ? 1 : 0, scale: 1 }}
              exit={{ opacity: 0, scale: 0.97 }}
              transition={{ duration: 0.12 }}
              style={{
                position: 'fixed',
                top: placement?.top ?? 0,
                left: placement?.left ?? 0,
              }}
              className={cn(
                'pointer-events-none z-tip flex max-w-[260px] items-center gap-2 rounded-lg bg-ink px-2.5 py-1.5 text-[12px] font-medium leading-snug text-canvas',
                'shadow-[0_6px_20px_-4px_rgb(0_0_0/0.35)] dark:bg-raised dark:text-ink dark:ring-1 dark:ring-white/10',
              )}
            >
              <span className="min-w-0 break-words">{label}</span>
              {hint && (
                <kbd className="shrink-0 rounded bg-canvas/15 px-1 font-mono text-[10.5px] dark:bg-ink/10">
                  {hint}
                </kbd>
              )}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  );
}
