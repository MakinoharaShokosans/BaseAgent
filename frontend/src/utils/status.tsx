/** 任务状态与连接状态的统一呈现：文字、色调、图标三者同时表达（不只靠颜色）。 */
import type { ReactNode } from 'react';
import type { ConnectionState } from '../api/sse';
import type { TaskStatus } from '../types/api';
import {
  CheckDraw,
  CrossGlyph,
  GuardGlyph,
  HandGlyph,
  OrbitGlyph,
} from '../components/icons/Glyphs';
import type { Tone } from '../components/ui/primitives';

export interface StatusMeta {
  label: string;
  tone: Tone;
  icon: ReactNode;
  live: boolean;
}

export function statusMeta(status: TaskStatus | string): StatusMeta {
  switch (status) {
    case 'queued':
      return { label: '排队中', tone: 'slate', icon: <OrbitGlyph size={14} />, live: true };
    case 'running':
      return { label: '运行中', tone: 'blue', icon: <OrbitGlyph size={14} />, live: true };
    case 'waiting_for_approval':
      return { label: '等待审批', tone: 'amber', icon: <HandGlyph size={14} />, live: false };
    case 'succeeded':
      return { label: '已完成', tone: 'green', icon: <CheckDraw size={14} />, live: false };
    case 'failed':
      return { label: '失败', tone: 'red', icon: <CrossGlyph size={14} />, live: false };
    case 'cancelled':
      return { label: '已取消', tone: 'slate', icon: <CrossGlyph size={14} />, live: false };
    case 'terminated':
      return { label: '已终止', tone: 'amber', icon: <GuardGlyph size={14} />, live: false };
    default:
      return { label: status, tone: 'slate', icon: null, live: false };
  }
}

export const CONNECTION_LABEL: Record<ConnectionState, string> = {
  connecting: '连接中',
  open: '已连接',
  reconnecting: '重连中',
  closed: '未连接',
  gap: '需要同步',
};

/** 连接是否"健康"：未订阅（closed）不算异常 */
export function connectionHealthy(state: ConnectionState): boolean {
  return state === 'open' || state === 'closed' || state === 'connecting';
}
