/**
 * 审批卡片。设计见 docs/10_frontend/02_approval_and_safety_ux.md §1。
 * 完整命令不截断；批准不是默认焦点；危险模式延迟启用批准；“当前会话免审”要二次确认；
 * 提交后立即禁用并显示“已提交”。
 */
import { AlertTriangle, Terminal } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ApprovalRequest, PermissionLevel } from '../../types/api';
import { cn, dangerLabels, stripControl } from '../../utils/text';
import { HandGlyph } from '../icons/Glyphs';
import { Checkbox } from '../ui/Checkbox';
import { CopyButton } from '../ui/CopyButton';
import { Badge, Button } from '../ui/primitives';

const ACTION_LABELS: Record<string, string> = {
  read: '读取',
  workspace_write: '工作区写入',
  network_egress: '网络外联',
  global_env: '修改全局环境',
  privileged: '提权操作',
  unknown: '未分类',
};

const LEVEL_LABELS: Record<PermissionLevel, string> = {
  read_only: '只读',
  workspace_write: '工作区写入',
  full_permissions: '全权限',
};

const REJECT_PRESETS = ['只在本地生成补丁，不要推送', '不要联网', '换一种方式完成'];
const DANGER_DELAY_MS = 1500;

interface HitlApprovalCardProps {
  request: ApprovalRequest;
  onApprove: (decision: 'once' | 'always') => void | Promise<void>;
  onReject: (reason: string) => void | Promise<void>;
  /** 测试用：覆盖延迟 */
  dangerDelayMs?: number;
}

export function HitlApprovalCard({
  request,
  onApprove,
  onReject,
  dangerDelayMs = DANGER_DELAY_MS,
}: HitlApprovalCardProps) {
  const labels = dangerLabels(request.command);
  const dangerous = labels.length > 0;
  const [armed, setArmed] = useState(!dangerous);
  const [submitted, setSubmitted] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');
  const [always, setAlways] = useState(false);
  const [confirmAlways, setConfirmAlways] = useState(false);
  const rejectRef = useRef<HTMLButtonElement>(null);
  // 同一帧内的连续点击读到的是旧的 state；用 ref 同步拦截重复提交
  const inflight = useRef(false);

  useEffect(() => {
    inflight.current = false;
    setSubmitted(false);
    setRejecting(false);
    setAlways(false);
    setConfirmAlways(false);
    setArmed(!dangerous);
    if (!dangerous) return;
    const timer = setTimeout(() => setArmed(true), dangerDelayMs);
    return () => clearTimeout(timer);
  }, [request.approval_id, dangerous, dangerDelayMs]);

  useEffect(() => {
    rejectRef.current?.focus(); // 默认焦点放在“拒绝”，回车不会直接批准
  }, [request.approval_id]);

  const command = stripControl(request.command);
  const lines = command.split('\n');
  const [expanded, setExpanded] = useState(lines.length <= 8);
  const shown = expanded ? command : lines.slice(0, 8).join('\n');
  const hiddenOthers = Math.max(request.escalation_count - 1 - request.related_actions.length, 0);

  async function approve(decision: 'once' | 'always') {
    if (inflight.current) return;
    inflight.current = true;
    setSubmitted(true);
    await onApprove(decision);
  }
  async function reject() {
    if (inflight.current) return;
    inflight.current = true;
    setSubmitted(true);
    await onReject(reason.trim() || '用户拒绝了该操作');
  }

  return (
    <section
      aria-label="需要审批的操作"
      className={cn(
        'relative overflow-hidden rounded-2xl border bg-card/95 p-4 sm:p-5 shadow-float backdrop-blur-xl transition-[border-color,background-color,box-shadow] duration-200',
        dangerous
          ? 'border-bad/40 shadow-[0_4px_24px_-4px_rgba(239,68,68,0.12)]'
          : 'border-warn/40 shadow-[0_4px_24px_-4px_rgba(245,158,11,0.10)]',
      )}
    >
      {/* 顶部高光微渐变线 */}
      <div
        aria-hidden
        className={cn(
          'absolute inset-x-0 top-0 h-[2px] transition-colors',
          dangerous
            ? 'bg-gradient-to-r from-transparent via-bad/80 to-transparent'
            : 'bg-gradient-to-r from-transparent via-warn/80 to-transparent',
        )}
      />

      {/* 头部：图标、操作类型与权限提权指示 */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <span
            aria-hidden
            className={cn(
              'flex h-8 w-8 shrink-0 items-center justify-center rounded-xl border transition-colors',
              dangerous
                ? 'border-bad/30 bg-bad/12 text-bad'
                : 'animate-halo border-warn/30 bg-warn/12 text-warn',
            )}
          >
            <HandGlyph size={16} />
          </span>

          <Badge tone={dangerous ? 'red' : 'amber'}>
            {ACTION_LABELS[request.action_type] ?? request.action_type}
          </Badge>

          <span className="text-[13px] font-semibold text-ink">
            需要{LEVEL_LABELS[request.required_level]}权限，当前为
            {LEVEL_LABELS[request.current_level]}
          </span>
        </div>

        {request.escalation_count > 1 && (
          <span className="rounded-full border border-ink/[0.08] bg-ink/[0.04] px-2 py-0.5 font-mono text-2xs text-mute dark:border-white/[0.08] dark:bg-white/[0.06]">
            关联 {request.escalation_count} 项操作
          </span>
        )}
      </div>

      {dangerous && (
        <div
          role="alert"
          className="mt-3 flex items-start gap-2.5 rounded-xl border border-bad/25 bg-bad/[0.08] px-3.5 py-2.5 text-sm font-medium text-bad"
        >
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-bad" />
          <span>高危操作：{labels.join('、')}。请逐字核对下面的命令。</span>
        </div>
      )}

      {request.reason && (
        <p className="mt-2.5 text-[13.5px] leading-relaxed text-ink/90">{request.reason}</p>
      )}

      {/* 终端命令展示卡片 */}
      <div className="mt-3.5 overflow-hidden rounded-xl border border-ink/[0.08] bg-code/95 dark:border-white/[0.08]">
        <div className="flex items-center justify-between border-b border-ink/[0.06] bg-ink/[0.02] px-3 py-1.5 text-2xs text-mute dark:border-white/[0.06] dark:bg-white/[0.02]">
          <div className="flex items-center gap-1.5 font-mono font-medium">
            <Terminal size={12} className="text-mute/80" />
            <span>待执行命令</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="tabular-nums">
              共 {command.length} 个字符、{lines.length} 行
            </span>
            <CopyButton text={command} label="复制命令" className="h-5 w-5" />
            {lines.length > 8 && (
              <button
                className="font-medium text-ink underline-offset-2 hover:underline"
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded ? '折叠' : '展开全部'}
              </button>
            )}
          </div>
        </div>

        <pre
          aria-label="待执行的完整命令"
          className="max-h-72 overflow-auto whitespace-pre-wrap break-all p-3.5 font-mono text-xs leading-relaxed text-code-ink"
        >
          {shown}
        </pre>
      </div>

      {(request.related_actions.length > 0 || request.escalation_count > 1) && (
        <div className="mt-3 rounded-xl border border-ink/[0.06] bg-canvas/60 px-3.5 py-2.5 text-xs text-ink shadow-sm dark:border-white/[0.06]">
          <p className="font-semibold text-ink">本次批准将同时放行这 {request.escalation_count} 项：</p>
          <ul className="mt-1 space-y-0.5 list-disc pl-4 text-mute">
            <li className="font-mono text-ink/90">{lines[0]}</li>
            {request.related_actions.map((a) => (
              <li key={a} className="font-mono">{a}</li>
            ))}
          </ul>
          {hiddenOthers > 0 && <p className="mt-1 text-mute/80">另有 {hiddenOthers} 项未列出</p>}
        </div>
      )}

      {/* 底部交互区 */}
      {rejecting ? (
        <div className="mt-4 space-y-2.5 rounded-xl border border-ink/[0.06] bg-canvas/50 p-3.5 dark:border-white/[0.06]">
          <p className="text-xs font-medium text-mute">
            拒绝理由会作为观察值回给模型，请写它应当照做的指示：
          </p>
          <div className="flex flex-wrap gap-1.5">
            {REJECT_PRESETS.map((p) => (
              <button
                key={p}
                className="rounded-full border border-ink/[0.08] bg-card px-2.5 py-1 text-xs text-ink/80 shadow-card transition-all hover:bg-raised hover:text-ink dark:border-white/[0.08]"
                onClick={() => setReason(p)}
              >
                {p}
              </button>
            ))}
          </div>
          <textarea
            aria-label="拒绝理由"
            className="field"
            rows={2}
            maxLength={1000}
            placeholder="可留空，默认“用户拒绝了该操作”"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="flex items-center gap-2 pt-1">
            <Button variant="danger" disabled={submitted} onClick={reject}>
              {submitted ? '已提交' : '确认拒绝'}
            </Button>
            <Button variant="ghost" disabled={submitted} onClick={() => setRejecting(false)}>
              返回
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-4 space-y-3">
          <div className="rounded-xl border border-ink/[0.06] bg-canvas/50 p-3 space-y-2 dark:border-white/[0.06]">
            <Checkbox
              label="本会话内完全相同的命令不再询问"
              checked={always}
              disabled={submitted}
              onChange={(next) => {
                setAlways(next);
                setConfirmAlways(false);
              }}
            >
              本会话内完全相同的命令不再询问。将加入白名单的内容：
              <code className="ml-1 break-all rounded bg-ink/[0.05] dark:bg-white/[0.08] px-1 py-0.5 font-mono text-xs">
                {lines[0]}
              </code>
            </Checkbox>

            {always && (
              <Checkbox
                label="我确认以后同样的命令会被直接执行"
                tone="danger"
                checked={confirmAlways}
                disabled={submitted}
                onChange={setConfirmAlways}
                className="font-semibold"
              >
                我确认以后同样的命令会被直接执行
              </Checkbox>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2 pt-1">
            <Button
              variant="secondary"
              disabled={submitted}
              onClick={() => setRejecting(true)}
              ref={rejectRef}
            >
              拒绝…
            </Button>
            <Button
              variant={dangerous ? 'danger' : 'primary'}
              disabled={submitted || !armed || (always && !confirmAlways)}
              onClick={() => approve(always ? 'always' : 'once')}
            >
              {submitted
                ? '已提交'
                : armed
                  ? always
                    ? '批准并免审'
                    : '批准本次'
                  : '请先核对命令…'}
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
