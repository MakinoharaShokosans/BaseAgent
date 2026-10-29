/** 外壳：环境层 + 侧栏（桌面固定 / 手机抽屉）+ 路由出口 + 全局提示。 */
import { Outlet, useRouterState } from '@tanstack/react-router';
import { AnimatePresence, MotionConfig, motion } from 'motion/react';
import { Copy, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useThemeEffect } from '../components/layout/ThemeToggle';
import { IconButton, SPRING } from '../components/ui/primitives';
import { Sidebar } from '../features/sidebar/Sidebar';
import { useAppStore } from '../stores/useAppStore';
import { useUiStore } from '../stores/useUiStore';
import { TERMINAL_STATUSES } from '../types/api';
import { cn } from '../utils/text';

/** 视口宽度断点。没有 matchMedia 的环境（jsdom）按宽屏处理。 */
export function useMinWidth(px: number): boolean {
  const query = `(min-width: ${px}px)`;
  const supported = typeof window.matchMedia === 'function';
  const [match, setMatch] = useState(() => (supported ? window.matchMedia(query).matches : true));
  useEffect(() => {
    if (!supported) return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatch(mql.matches);
    onChange();
    mql.addEventListener?.('change', onChange);
    return () => mql.removeEventListener?.('change', onChange);
  }, [query, supported]);
  return match;
}

function useShortcuts(): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
      const key = e.key.toLowerCase();
      if (key === 'b') {
        e.preventDefault();
        useUiStore.getState().toggleSidebar();
      } else if (key === 'j') {
        e.preventDefault();
        useUiStore.getState().toggleInspector();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}

function NoticeBar() {
  const notice = useAppStore((s) => s.notice);
  const clear = useAppStore((s) => s.clearNotice);
  const info = notice?.tone === 'info';
  // 成功反馈自动消失；错误提示留着，直到用户关掉
  useEffect(() => {
    if (!notice || !info) return;
    const timer = setTimeout(clear, 2200);
    return () => clearTimeout(timer);
  }, [notice, info, clear]);
  return (
    <AnimatePresence>
      {notice && (
        <motion.div
          role="alert"
          initial={{ opacity: 0, y: -16, x: '-50%' }}
          animate={{ opacity: 1, y: 0, x: '-50%' }}
          exit={{ opacity: 0, y: -12, x: '-50%' }}
          transition={SPRING}
          className={cn(
            'fixed left-1/2 top-4 z-toast flex max-w-[min(36rem,calc(100vw-1.5rem))] items-center gap-3 rounded-2xl border border-ink/[0.08] bg-raised/95 py-2 pl-4 pr-2 text-sm shadow-float backdrop-blur-2xl dark:border-white/[0.08]',
            info ? 'text-ink' : 'text-bad',
          )}
        >
          <span
            className={cn(
              'h-2 w-2 shrink-0 rounded-full',
              info
                ? 'bg-ok shadow-[0_0_8px_rgb(var(--c-ok)/0.5)]'
                : 'bg-bad shadow-[0_0_8px_rgb(var(--c-bad)/0.5)]',
            )}
          />
          <span className="min-w-0 flex-1 break-words text-ink">{notice.message}</span>
          {notice.traceId && (
            <IconButton
              label={`复制 trace_id ${notice.traceId}`}
              onClick={() => void navigator.clipboard?.writeText(notice.traceId as string)}
            >
              <Copy size={14} />
            </IconButton>
          )}
          <IconButton label="关闭" onClick={clear}>
            <X size={15} />
          </IconButton>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function Shell() {
  useThemeEffect();
  useShortcuts();
  const live = useAppStore((s) => !!s.run && !TERMINAL_STATUSES.includes(s.run.status));
  const collapsed = useUiStore((s) => s.sidebarCollapsed);
  const navOpen = useUiStore((s) => s.navOpen);
  const setNavOpen = useUiStore((s) => s.setNavOpen);
  const setNarrow = useUiStore((s) => s.setNarrow);
  const setPhone = useUiStore((s) => s.setPhone);
  const phone = !useMinWidth(768);
  const wide = useMinWidth(1100);
  const inSettings = useRouterState({ select: (s) => s.location.pathname.startsWith('/settings') });
  useEffect(() => setPhone(phone), [phone, setPhone]);
  useEffect(() => setNarrow(!wide), [wide, setNarrow]);

  return (
    <MotionConfig reducedMotion="user">
      <div className="relative flex h-full overflow-hidden bg-canvas">
        <div aria-hidden className="ambient" data-live={live} />
        <div aria-hidden className="grain" />

        {phone ? (
          <AnimatePresence>
            {navOpen && (
              <>
                <motion.div
                  key="scrim"
                  aria-hidden
                  className="fixed inset-0 z-drawer bg-canvas/60 backdrop-blur-sm"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  onClick={() => setNavOpen(false)}
                />
                <motion.div
                  key="nav"
                  className="fixed inset-y-0 left-0 z-modal shadow-float"
                  initial={{ x: -300 }}
                  animate={{ x: 0 }}
                  exit={{ x: -300 }}
                  transition={SPRING}
                >
                  <Sidebar />
                </motion.div>
              </>
            )}
          </AnimatePresence>
        ) : inSettings ? null : (
          <Sidebar collapsedRail={collapsed} />
        )}

        <main className="relative z-0 flex min-w-0 flex-1 flex-col">
          <Outlet />
        </main>
        <NoticeBar />
      </div>
    </MotionConfig>
  );
}
