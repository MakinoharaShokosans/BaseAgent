/** 最近一次停留的位置，只用于 `/` 的默认去向；不是业务状态。 */
const LAST = 'baseagent.last.v1';

export interface LastLocation {
  workspaceId: string;
  sessionId?: string;
}

export function rememberLocation(workspaceId: string, sessionId?: string): void {
  try {
    window.localStorage.setItem(LAST, JSON.stringify({ workspaceId, sessionId }));
  } catch {
    // 隐私模式或存储被禁用：不记住
  }
}

export function lastLocation(): LastLocation | null {
  try {
    const raw = window.localStorage.getItem(LAST);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<LastLocation>;
    return typeof parsed.workspaceId === 'string'
      ? { workspaceId: parsed.workspaceId, sessionId: parsed.sessionId }
      : null;
  } catch {
    return null;
  }
}
