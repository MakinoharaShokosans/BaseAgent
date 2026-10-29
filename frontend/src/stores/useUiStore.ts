/** 纯界面偏好：侧栏、检视器、卡片折叠、主题。只存浏览器本地，读写失败时回落为缺省值。 */
import { create } from 'zustand';

export type ThemePref = 'system' | 'light' | 'dark';
export type ThinkingPref = 'expanded' | 'collapsed' | 'hidden';

interface Prefs {
  sidebarCollapsed: boolean;
  inspectorOpen: boolean;
  /** 已折叠的右侧卡片 id */
  collapsedCards: string[];
  theme: ThemePref;
  /** 思考链的默认显示方式 */
  thinking: ThinkingPref;
  /** 侧栏里展开的工作区 */
  openWorkspaces: string[];
  /** 用户是否手动开合过工作区；没动过时首次进入自动展开当前工作区 */
  workspacesTouched: boolean;
}

interface UiState extends Prefs {
  /** 窄屏时检视器是覆盖式抽屉，开合不持久化、缺省关闭 */
  narrow: boolean;
  drawerOpen: boolean;
  /** 手机宽度：侧栏是覆盖式抽屉 */
  phone: boolean;
  navOpen: boolean;
  setNarrow: (value: boolean) => void;
  setPhone: (value: boolean) => void;
  setNavOpen: (value: boolean) => void;
  setThinking: (value: ThinkingPref) => void;
  setWorkspaceOpen: (id: string, open: boolean) => void;
  toggleSidebar: () => void;
  setSidebarCollapsed: (value: boolean) => void;
  toggleInspector: () => void;
  toggleCard: (id: string) => void;
  openCard: (id: string) => void;
  setTheme: (theme: ThemePref) => void;
  /** 侧栏底部的按钮：在浅色与暗色之间切换（"跟随系统"时按当前实际明暗翻转） */
  toggleTheme: () => void;
}

const KEY = 'baseagent.ui.v2';
const DEFAULTS: Prefs = {
  sidebarCollapsed: false,
  inspectorOpen: true,
  collapsedCards: [],
  theme: 'light', // 浅色优先，见 03_visual_design.md §1
  thinking: 'expanded',
  openWorkspaces: [],
  workspacesTouched: false,
};

function load(): Prefs {
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? { ...DEFAULTS, ...(JSON.parse(raw) as Partial<Prefs>) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

function save(state: Prefs): void {
  try {
    const {
      sidebarCollapsed,
      inspectorOpen,
      collapsedCards,
      theme,
      thinking,
      openWorkspaces,
      workspacesTouched,
    } = state;
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        sidebarCollapsed,
        inspectorOpen,
        collapsedCards,
        theme,
        thinking,
        openWorkspaces,
        workspacesTouched,
      }),
    );
  } catch {
    // 隐私模式或存储被禁用：偏好只在本次页面内生效
  }
}

/** 系统是否偏好暗色。jsdom 等环境没有 matchMedia 时按亮色处理。 */
function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)').matches
    : false;
}

/** 宽度断点：初值同步算出，否则手机首帧会按桌面布局渲染一次（设置页会误跳详情）。 */
function wideEnough(px: number): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(`(min-width: ${px}px)`).matches
    : true;
}

export function resolvedDark(theme: ThemePref): boolean {
  return theme === 'dark' || (theme === 'system' && systemPrefersDark());
}

export const useUiStore = create<UiState>((set) => ({
  ...load(),
  narrow: !wideEnough(1100),
  drawerOpen: false,
  phone: !wideEnough(768),
  navOpen: false,
  setNarrow: (value) => set({ narrow: value, drawerOpen: false }),
  setPhone: (value) => set({ phone: value, navOpen: false }),
  setNavOpen: (value) => set({ navOpen: value }),
  setThinking: (thinking) => set({ thinking }),
  setWorkspaceOpen: (id, open) =>
    set((s) => ({
      workspacesTouched: true,
      openWorkspaces: open
        ? s.openWorkspaces.includes(id)
          ? s.openWorkspaces
          : [...s.openWorkspaces, id]
        : s.openWorkspaces.filter((w) => w !== id),
    })),
  toggleSidebar: () =>
    set((s) => (s.phone ? { navOpen: !s.navOpen } : { sidebarCollapsed: !s.sidebarCollapsed })),
  setSidebarCollapsed: (value) => set({ sidebarCollapsed: value }),
  toggleInspector: () =>
    set((s) => (s.narrow ? { drawerOpen: !s.drawerOpen } : { inspectorOpen: !s.inspectorOpen })),
  toggleCard: (id) =>
    set((s) => ({
      collapsedCards: s.collapsedCards.includes(id)
        ? s.collapsedCards.filter((c) => c !== id)
        : [...s.collapsedCards, id],
    })),
  openCard: (id) => set((s) => ({ collapsedCards: s.collapsedCards.filter((c) => c !== id) })),
  setTheme: (theme) => set({ theme }),
  toggleTheme: () => set((s) => ({ theme: resolvedDark(s.theme) ? 'light' : 'dark' })),
}));

useUiStore.subscribe(save);
