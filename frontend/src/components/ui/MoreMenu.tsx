/**
 * “更多操作”菜单：一个三点按钮，点开一列操作。
 * 菜单渲染到 body 并按触发按钮定位：侧栏有 overflow 裁切与 backdrop-filter，
 * 放在原地会被裁掉或被困在侧栏的包含块里。
 */
import { MoreHorizontal } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../../utils/text';
import { IconButton } from './primitives';

export interface MenuItem {
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  /** 在此项之前画一条分隔线 */
  separated?: boolean;
}

const MENU_WIDTH = 188;
const GAP = 4;

export function MoreMenu({
  label,
  items,
  className,
  onOpenChange,
}: {
  label: string;
  items: MenuItem[];
  className?: string;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    onOpenChange?.(false);
    trigger.current?.focus();
  }, [onOpenChange]);

  // 按触发按钮定位：右缘对齐，放不下就翻到按钮上方，左右不出视口
  useLayoutEffect(() => {
    if (!open || !trigger.current) return;
    const r = trigger.current.getBoundingClientRect();
    const height = menu.current?.offsetHeight ?? items.length * 34 + 12;
    const left = Math.max(8, Math.min(r.right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - 8));
    const below = r.bottom + GAP;
    const top = below + height > window.innerHeight - 8 ? Math.max(8, r.top - GAP - height) : below;
    setPos({ left, top });
  }, [open, items.length]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!menu.current?.contains(t) && !trigger.current?.contains(t)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close();
      }
    };
    const dismiss = () => {
      setOpen(false);
      onOpenChange?.(false);
    };
    window.addEventListener('pointerdown', onDown, true);
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', dismiss);
    window.addEventListener('scroll', dismiss, true);
    return () => {
      window.removeEventListener('pointerdown', onDown, true);
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', dismiss);
      window.removeEventListener('scroll', dismiss, true);
    };
  }, [open, close, onOpenChange]);

  // 打开后焦点进到第一项，方向键在各项之间移动
  useEffect(() => {
    if (open && pos)
      menu.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
  }, [open, pos]);

  const onMenuKey = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const buttons = [
      ...(menu.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? []),
    ];
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = (at + (e.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  };

  return (
    <>
      <IconButton
        ref={trigger}
        label={label}
        active={open}
        aria-haspopup="menu"
        aria-expanded={open}
        className={className}
        onClick={(e) => {
          e.stopPropagation();
          setPos(null);
          const next = !open;
          setOpen(next);
          onOpenChange?.(next);
        }}
      >
        <MoreHorizontal size={15} />
      </IconButton>
      {open &&
        createPortal(
          <div
            ref={menu}
            role="menu"
            aria-label={label}
            onKeyDown={onMenuKey}
            style={{
              position: 'fixed',
              left: pos?.left ?? 0,
              top: pos?.top ?? 0,
              width: MENU_WIDTH,
              visibility: pos ? 'visible' : 'hidden',
            }}
            className="z-[60] rounded-2xl bg-raised p-1.5 shadow-float"
          >
            {items.map((item) => (
              <div key={item.label}>
                {item.separated && <div className="mx-2 my-1 h-px bg-ink/[0.07]" />}
                <button
                  role="menuitem"
                  disabled={item.disabled}
                  className={cn(
                    'flex w-full items-center gap-2.5 rounded-xl px-2.5 py-1.5 text-left text-[13px] outline-none transition-colors',
                    'hover:bg-ink/[0.06] focus-visible:bg-ink/[0.06] disabled:cursor-not-allowed disabled:opacity-40',
                    item.danger ? 'text-bad' : 'text-ink',
                  )}
                  onClick={() => {
                    setOpen(false);
                    onOpenChange?.(false);
                    item.onSelect();
                  }}
                >
                  {item.icon && <span className="shrink-0 opacity-80">{item.icon}</span>}
                  <span className="min-w-0 flex-1 truncate">{item.label}</span>
                </button>
              </div>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
