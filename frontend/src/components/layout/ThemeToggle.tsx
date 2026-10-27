import { AnimatePresence, motion } from 'motion/react';
import { Moon, Sun } from 'lucide-react';
import { useEffect } from 'react';
import { resolvedDark, useUiStore } from '../../stores/useUiStore';
import { IconButton } from '../ui/primitives';

/** 把主题偏好落到 <html data-theme>，并在"跟随系统"时监听系统切换 */
export function useThemeEffect(): void {
  const theme = useUiStore((s) => s.theme);
  useEffect(() => {
    const apply = () => {
      document.documentElement.dataset.theme = resolvedDark(theme) ? 'dark' : 'light';
    };
    apply();
    if (theme !== 'system' || typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    query.addEventListener?.('change', apply);
    return () => query.removeEventListener?.('change', apply);
  }, [theme]);
}

/** 侧栏底部：浅色 / 暗色切换 */
export function ThemeToggle() {
  const theme = useUiStore((s) => s.theme);
  const toggle = useUiStore((s) => s.toggleTheme);
  const dark = resolvedDark(theme);
  const Icon = dark ? Moon : Sun;
  return (
    <IconButton
      label={dark ? '切换到浅色' : '切换到暗色'}
      onClick={toggle}
      className="overflow-hidden"
    >
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span
          key={dark ? 'dark' : 'light'}
          initial={{ y: 14, rotate: -40, opacity: 0 }}
          animate={{ y: 0, rotate: 0, opacity: 1 }}
          exit={{ y: -14, rotate: 40, opacity: 0 }}
          transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
          className="flex"
        >
          <Icon size={17} strokeWidth={1.7} />
        </motion.span>
      </AnimatePresence>
    </IconButton>
  );
}
