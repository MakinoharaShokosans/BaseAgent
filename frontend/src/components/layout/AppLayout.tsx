/**
 * 外壳：L0 环境层 + 侧栏 + 顶栏/对话 + 检视器 + 提示。
 * 布局、层次与响应式规则见 docs/10_frontend/03_visual_design.md §4–§5。
 */
import { AnimatePresence, MotionConfig, motion } from 'motion/react';
import { Copy, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { useUiStore } from '../../stores/useUiStore';
import { TERMINAL_STATUSES } from '../../types/api';
import { ChatPane } from '../chat/ChatPane';
import { Inspector } from '../inspector/Inspector';
import { Sidebar } from '../sidebar/Sidebar';
import { IconButton, SPRING } from '../ui/primitives';
import { useThemeEffect } from './ThemeToggle';
import { TopBar } from './TopBar';

/** 视口宽度断点。没有 matchMedia 的环境（jsdom）按宽屏处理。 */
function useMinWidth(px: number): boolean {
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
  return (
    <AnimatePresence>
      {notice && (
        <motion.div
          role="alert"
          initial={{ opacity: 0, y: -16, x: '-50%' }}
          animate={{ opacity: 1, y: 0, x: '-50%' }}
          exit={{ opacity: 0, y: -12, x: '-50%' }}
          transition={SPRING}
          className="absolute left-1/2 top-3 z-50 flex max-w-xl items-center gap-3 rounded-2xl bg-raised/95 py-2 pl-4 pr-2 text-sm text-bad shadow-float backdrop-blur-xl"
        >
          <span className="h-2 w-2 shrink-0 rounded-full bg-bad" />
          <span className="min-w-0 flex-1 text-ink">{notice.message}</span>
          {notice.traceId && (
            <button
              className="flex shrink-0 items-center gap-1 rounded-lg px-2 py-1 text-xs font-semibold text-mute hover:bg-ink/[0.06] hover:text-ink"
              title={notice.traceId}
              onClick={() => void navigator.clipboard?.writeText(notice.traceId as string)}
            >
              <Copy size={12} /> 复制 trace_id
            </button>
          )}
          <IconButton label="关闭提示" onClick={clear}>
            <X size={15} />
          </IconButton>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function AppLayout() {
  useThemeEffect();
  useShortcuts();
  const live = useAppStore((s) => !!s.run && !TERMINAL_STATUSES.includes(s.run.status));
  const inspectorOpen = useUiStore((s) => s.inspectorOpen);
  const drawerOpen = useUiStore((s) => s.drawerOpen);
  const setNarrow = useUiStore((s) => s.setNarrow);
  const toggleInspector = useUiStore((s) => s.toggleInspector);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);
  const sidebarCollapsed = useUiStore((s) => s.sidebarCollapsed);
  const wide = useMinWidth(1100);
  const roomy = useMinWidth(1280);
  const phone = !useMinWidth(768);
  const inspectorWidth = roomy ? 352 : 320;
  useEffect(() => setNarrow(!wide), [wide, setNarrow]);

  return (
    <MotionConfig reducedMotion="user">
      <div className="relative flex h-full overflow-hidden bg-canvas">
        <div aria-hidden className="ambient" data-live={live} />
        <div aria-hidden className="grain" />

        {phone ? (
          <AnimatePresence>
            {!sidebarCollapsed && (
              <>
                <motion.div
                  key="nav-scrim"
                  aria-hidden
                  className="fixed inset-0 z-30 bg-canvas/60 backdrop-blur-sm"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  onClick={toggleSidebar}
                />
                <motion.div
                  key="nav"
                  className="fixed inset-y-0 left-0 z-40 shadow-float"
                  initial={{ x: -280 }}
                  animate={{ x: 0 }}
                  exit={{ x: -280 }}
                  transition={SPRING}
                >
                  <Sidebar />
                </motion.div>
              </>
            )}
          </AnimatePresence>
        ) : (
          <Sidebar />
        )}

        <main className="relative z-0 flex min-w-0 flex-1 flex-col">
          <TopBar onOpenNav={phone ? toggleSidebar : undefined} />
          <div className="min-h-0 flex-1">
            <ChatPane />
          </div>
        </main>

        {wide ? (
          <motion.aside
            aria-label="检视器"
            initial={false}
            animate={{ width: inspectorOpen ? inspectorWidth : 0, opacity: inspectorOpen ? 1 : 0 }}
            transition={SPRING}
            className="relative z-10 shrink-0 overflow-hidden"
          >
            <div style={{ width: inspectorWidth }} className="h-full">
              {inspectorOpen && <Inspector />}
            </div>
          </motion.aside>
        ) : (
          <AnimatePresence>
            {drawerOpen && (
              <>
                <motion.div
                  key="inspector-scrim"
                  aria-hidden
                  className="fixed inset-0 z-30 bg-canvas/50 backdrop-blur-sm"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  onClick={toggleInspector}
                />
                <motion.aside
                  key="inspector"
                  aria-label="检视器"
                  className="fixed inset-y-0 right-0 z-40 w-[min(352px,92vw)] bg-canvas/95 shadow-float backdrop-blur-xl"
                  initial={{ x: 360 }}
                  animate={{ x: 0 }}
                  exit={{ x: 360 }}
                  transition={SPRING}
                >
                  <Inspector />
                </motion.aside>
              </>
            )}
          </AnimatePresence>
        )}
        <NoticeBar />
      </div>
    </MotionConfig>
  );
}
