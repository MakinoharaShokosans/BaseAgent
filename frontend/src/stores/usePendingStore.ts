import { create } from 'zustand';

/** 已保存但还没被任务采纳的改动数（MCP、技能、提示词、工作区设置在下个任务才生效） */
interface PendingState {
  count: number;
  mark: () => void;
  clear: () => void;
}

export const usePendingStore = create<PendingState>((set) => ({
  count: 0,
  mark: () => set((s) => ({ count: s.count + 1 })),
  clear: () => set({ count: 0 }),
}));
