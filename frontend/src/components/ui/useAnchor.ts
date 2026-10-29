/**
 * 把浮层定位到触发元素旁边：fixed 定位 + Portal，不受祖先 overflow / transform 影响。
 * 空间不够时翻转到另一侧，并夹在视口内。
 */
import { useLayoutEffect, useState, type RefObject } from 'react';

export type Side = 'top' | 'bottom' | 'left' | 'right';

export interface Placement {
  top: number;
  left: number;
  side: Side;
  /** 触发元素宽度，菜单可用它做最小宽度 */
  width: number;
}

const GAP = 8;
const MARGIN = 8;

export function computePlacement(
  anchor: DOMRect,
  pop: { width: number; height: number },
  prefer: Side,
  align: 'start' | 'center' | 'end',
  viewport = { width: window.innerWidth, height: window.innerHeight },
): Placement {
  const room: Record<Side, number> = {
    top: anchor.top,
    bottom: viewport.height - anchor.bottom,
    left: anchor.left,
    right: viewport.width - anchor.right,
  };
  const need = prefer === 'top' || prefer === 'bottom' ? pop.height : pop.width;
  const opposite: Record<Side, Side> = {
    top: 'bottom',
    bottom: 'top',
    left: 'right',
    right: 'left',
  };
  const side =
    room[prefer] >= need + GAP || room[prefer] >= room[opposite[prefer]]
      ? prefer
      : opposite[prefer];

  let top: number;
  let left: number;
  if (side === 'top' || side === 'bottom') {
    top = side === 'top' ? anchor.top - pop.height - GAP : anchor.bottom + GAP;
    left =
      align === 'start'
        ? anchor.left
        : align === 'end'
          ? anchor.right - pop.width
          : anchor.left + anchor.width / 2 - pop.width / 2;
  } else {
    left = side === 'left' ? anchor.left - pop.width - GAP : anchor.right + GAP;
    top =
      align === 'start'
        ? anchor.top
        : align === 'end'
          ? anchor.bottom - pop.height
          : anchor.top + anchor.height / 2 - pop.height / 2;
  }
  left = Math.min(Math.max(left, MARGIN), Math.max(MARGIN, viewport.width - pop.width - MARGIN));
  top = Math.min(Math.max(top, MARGIN), Math.max(MARGIN, viewport.height - pop.height - MARGIN));
  return { top, left, side, width: anchor.width };
}

/** 打开期间跟随触发元素；滚动与缩放时重算。 */
export function useAnchor(
  open: boolean,
  anchor: RefObject<HTMLElement | null>,
  pop: RefObject<HTMLElement | null>,
  prefer: Side = 'bottom',
  align: 'start' | 'center' | 'end' = 'start',
): Placement | null {
  const [placement, setPlacement] = useState<Placement | null>(null);

  useLayoutEffect(() => {
    if (!open) {
      setPlacement(null);
      return;
    }
    const update = () => {
      const a = anchor.current?.getBoundingClientRect();
      const p = pop.current?.getBoundingClientRect();
      if (!a || !p) return;
      setPlacement(computePlacement(a, { width: p.width, height: p.height }, prefer, align));
    };
    update();
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [open, anchor, pop, prefer, align]);

  return placement;
}
