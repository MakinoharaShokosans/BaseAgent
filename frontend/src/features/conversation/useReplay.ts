import { useEffect, useState } from 'react';
import { http } from '../../api/http';
import { subscribe } from '../../api/sse';
import { emptyRun, reduceTaskEvent, type RunState } from '../../stores/taskReducer';

/** 回放一个已结束任务的全部事件（事件库里有完整历史，流在终态后自行关闭） */
export function useReplay(taskId: string, goal: string, enabled: boolean): RunState | null {
  const [run, setRun] = useState<RunState | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let state = emptyRun(taskId, goal, 'succeeded');
    const handle = subscribe({
      url: http.url(`/tasks/${encodeURIComponent(taskId)}/stream`),
      headers: () => http.headers(),
      onEvent: (event) => {
        state = reduceTaskEvent(state, event);
        setRun(state);
      },
    });
    return () => handle.close();
  }, [taskId, goal, enabled]);
  return run;
}
