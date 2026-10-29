/** 一次任务的运行视图：思考、汇报、工具调用、审批、交付，按事件顺序排列。 */
import { Download, GitFork, Play } from 'lucide-react';
import { useMemo } from 'react';
import { api } from '../../api/endpoints';
import { SafeMarkdown } from '../../components/common/SafeMarkdown';
import { HitlApprovalCard } from '../../components/domain/HitlApprovalCard';
import { CopyButton } from '../../components/ui/CopyButton';
import { IconButton } from '../../components/ui/primitives';
import { useAppStore } from '../../stores/useAppStore';
import type { RunState, TimelineItem } from '../../stores/taskReducer';
import { TERMINAL_STATUSES } from '../../types/api';
import { cn } from '../../utils/text';
import { formatDuration } from '../../utils/time';
import { useNow } from '../../utils/useNow';
import { AgentGroup } from './blocks/AgentGroup';
import { ReasoningBlock } from './blocks/ReasoningBlock';
import { SayBlock } from './blocks/SayBlock';
import { ToolCard } from './blocks/ToolCard';

type AgentItem = Extract<TimelineItem, { kind: 'agent' }>;
type Node = { key: string; item: TimelineItem } | { key: string; agents: AgentItem[] };

/** 相邻的同类 agent 事件合并成一组 */
function nodesOf(items: TimelineItem[]): Node[] {
  const out: Node[] = [];
  for (const [index, item] of items.entries()) {
    const key = `${item.seq}-${index}`;
    const prev = out[out.length - 1];
    if (item.kind === 'agent' && prev && 'agents' in prev && prev.agents[0]?.group === item.group) {
      prev.agents.push(item);
    } else if (item.kind === 'agent') {
      out.push({ key, agents: [item] });
    } else {
      out.push({ key, item });
    }
  }
  return out;
}

export function RunView({
  run,
  live,
  onFork,
}: {
  run: RunState;
  /** 当前正在订阅的任务（可审批、可续跑）；回放的历史任务为 false */
  live: boolean;
  onFork?: () => void;
}) {
  const approve = useAppStore((s) => s.approve);
  const reject = useAppStore((s) => s.reject);
  const resume = useAppStore((s) => s.resume);
  const nodes = useMemo(() => nodesOf(run.items), [run.items]);
  const terminal = TERMINAL_STATUSES.includes(run.status);
  const working = !terminal && run.status !== 'waiting_for_approval';
  const streaming = run.items.some(
    (i) => (i.kind === 'reasoning' || i.kind === 'say') && i.streaming,
  );
  const now = useNow(working && !streaming);

  return (
    <div className="space-y-3">
      {nodes.map((n) => {
        if ('agents' in n) return <AgentGroup key={n.key} items={n.agents} />;
        const item = n.item;
        const turn = 'turn' in item ? item.turn : 'step' in item ? item.step : undefined;
        let content = null;
        switch (item.kind) {
          case 'reasoning':
            content = <ReasoningBlock item={item} />;
            break;
          case 'say':
            content = <SayBlock item={item} />;
            break;
          case 'tool':
            content = <ToolCard item={item} taskId={run.taskId} />;
            break;
          case 'decision':
            content = (
              <p className={cn('text-[12.5px]', item.approved ? 'text-ok' : 'text-warn')}>
                {item.approved ? '已批准' : `已拒绝：${item.text}`}
              </p>
            );
            break;
          case 'guard':
            content = <p className="text-[12.5px] text-warn">{item.text}</p>;
            break;
          case 'unknown':
            content = <p className="font-mono text-2xs text-mute/60">{item.name}</p>;
            break;
          default:
            content = null;
        }
        if (!content) return null;
        return (
          <div
            key={n.key}
            id={turn ? `turn-item-${turn}` : undefined}
            data-turn={turn}
            className="scroll-mt-6"
          >
            {content}
          </div>
        );
      })}

      {live && run.approval && (
        <HitlApprovalCard
          request={run.approval}
          onApprove={(decision) => approve(decision)}
          onReject={(reason) => reject(reason)}
        />
      )}

      {working && !streaming && (
        <div className="flex items-center gap-2 text-[12.5px] text-mute" role="status">
          <span className="flex gap-0.5" aria-hidden>
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="h-1 w-1 animate-dots rounded-full bg-accent"
                style={{ animationDelay: `${i * 0.18}s` }}
              />
            ))}
          </span>
          {run.startedTs && (
            <span className="tabular-nums">
              {formatDuration(Math.max(0, (now - run.startedTs) * 1000))}
            </span>
          )}
        </div>
      )}

      {run.error && <p className="text-[13px] text-bad">{run.error}</p>}

      {terminal && run.delivery && (
        <div className="rounded-2xl bg-card/80 px-4 py-3 shadow-card">
          <SafeMarkdown text={run.delivery} />
        </div>
      )}

      {terminal && (
        <div className="flex items-center gap-1 text-[12px] text-mute">
          <span className="tabular-nums">
            {run.stepCount} 回合 · {run.totalTokens.toLocaleString()} Token
            {run.startedTs && run.finishedTs
              ? ` · ${formatDuration((run.finishedTs - run.startedTs) * 1000)}`
              : ''}
          </span>
          {run.terminationReason && run.status !== 'succeeded' && (
            <span className="ml-2 min-w-0 truncate text-bad">{run.terminationReason}</span>
          )}
          <span className="ml-auto flex items-center">
            {run.delivery && <CopyButton text={run.delivery} label="复制交付" />}
            <IconButton
              label="下载轨迹"
              className="h-6 w-6"
              onClick={() => window.open(api.tasks.traceUrl(run.taskId))}
            >
              <Download size={13} />
            </IconButton>
            {onFork && (
              <IconButton label="从这里分支" className="h-6 w-6" onClick={onFork}>
                <GitFork size={13} />
              </IconButton>
            )}
            {live && run.status !== 'succeeded' && (
              <IconButton label="从检查点续跑" className="h-6 w-6" onClick={() => void resume()}>
                <Play size={13} />
              </IconButton>
            )}
          </span>
        </div>
      )}
    </div>
  );
}
