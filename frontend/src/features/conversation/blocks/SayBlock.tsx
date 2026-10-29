/** 模型每一步的汇报（已完成 / 下一步 / 进度）。正文按 Markdown 渲染，进度条只作展示。 */
import { SafeMarkdown } from '../../../components/common/SafeMarkdown';
import { parseSay, type TimelineItem } from '../../../stores/taskReducer';

type Item = Extract<TimelineItem, { kind: 'say' }>;

export function SayBlock({ item }: { item: Item }) {
  const { body, progress } = parseSay(item.text);
  return (
    <div className="text-[15px]">
      <SafeMarkdown text={body || item.text} />
      {item.streaming && (
        <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-pulse bg-accent/70" />
      )}
      {progress && (
        <div className="mt-2 flex items-center gap-2 text-2xs text-mute">
          <div className="h-[3px] w-24 overflow-hidden rounded-full bg-ink/[0.08]">
            <div
              className="h-full rounded-full bg-brand transition-[width] duration-500"
              style={{ width: `${progress.percent}%` }}
            />
          </div>
          <span className="tabular-nums">{progress.percent}%</span>
          {progress.label && <span className="truncate">{progress.label}</span>}
        </div>
      )}
    </div>
  );
}
