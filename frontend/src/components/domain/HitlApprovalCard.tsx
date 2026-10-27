/**
 * 审批卡片。设计见 docs/10_frontend/02_approval_and_safety_ux.md §1。
 * 完整命令不截断；批准不是默认焦点；危险模式延迟启用批准；“当前会话免审”要二次确认；
 * 提交后立即禁用并显示“已提交”。
 */
import { useEffect, useRef, useState } from 'react';
import type { ApprovalRequest, PermissionLevel } from '../../types/api';
import { dangerLabels, stripControl } from '../../utils/text';
import { HandGlyph } from '../icons/Glyphs';
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
export const DANGER_DELAY_MS = 1500;

export interface HitlApprovalCardProps {
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
      className={`relative overflow-hidden rounded-2xl border p-4 shadow-float ${dangerous ? 'border-bad/50 bg-bad/[0.06]' : 'border-warn/45 bg-warn/[0.06]'}`}
    >
      <span
        aria-hidden
        className={`absolute inset-y-0 left-0 w-1 ${dangerous ? 'bg-bad' : 'bg-warn'}`}
      />
      <div className="flex flex-wrap items-center gap-2">
        <span
          aria-hidden
          className={`flex h-7 w-7 items-center justify-center rounded-full ${dangerous ? 'bg-bad/15 text-bad' : 'animate-halo bg-warn/15 text-warn'}`}
        >
          <HandGlyph size={15} />
        </span>
        <Badge tone={dangerous ? 'red' : 'amber'}>
          {ACTION_LABELS[request.action_type] ?? request.action_type}
        </Badge>
        <span className="text-sm font-semibold text-ink">
          需要{LEVEL_LABELS[request.required_level]}权限，当前为
          {LEVEL_LABELS[request.current_level]}
        </span>
      </div>

      {dangerous && (
        <p
          role="alert"
          className="mt-3 rounded-xl bg-bad/10 px-3 py-2 text-sm font-semibold text-bad"
        >
          高危操作：{labels.join('、')}。请逐字核对下面的命令。
        </p>
      )}

      <p className="mt-2 text-sm text-ink">{request.reason}</p>

      <pre
        aria-label="待执行的完整命令"
        className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-code p-3.5 font-mono text-xs leading-relaxed text-code-ink ring-1 ring-inset ring-white/5"
      >
        {shown}
      </pre>
      <div className="mt-1 flex items-center justify-between text-xs text-mute">
        <span>
          共 {command.length} 个字符、{lines.length} 行
        </span>
        {lines.length > 8 && (
          <button
            className="font-semibold text-ink underline-offset-2 hover:underline"
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? '折叠' : '展开全部'}
          </button>
        )}
      </div>

      {(request.related_actions.length > 0 || request.escalation_count > 1) && (
        <div className="mt-3 rounded-xl bg-card/70 px-3 py-2 text-xs text-ink">
          <p className="font-medium">本次批准将同时放行这 {request.escalation_count} 项：</p>
          <ul className="list-disc pl-5">
            <li>{lines[0]}</li>
            {request.related_actions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
          {hiddenOthers > 0 && <p>另有 {hiddenOthers} 项未列出</p>}
        </div>
      )}

      {rejecting ? (
        <div className="mt-3 space-y-2">
          <p className="text-xs text-mute">拒绝理由会作为观察值回给模型，请写它应当照做的指示。</p>
          <div className="flex flex-wrap gap-1">
            {REJECT_PRESETS.map((p) => (
              <button
                key={p}
                className="rounded-full bg-card px-2.5 py-0.5 text-xs shadow-card transition-colors hover:bg-ink/[0.04]"
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
          <div className="flex gap-2">
            <Button variant="danger" disabled={submitted} onClick={reject}>
              {submitted ? '已提交' : '确认拒绝'}
            </Button>
            <Button variant="ghost" disabled={submitted} onClick={() => setRejecting(false)}>
              返回
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-3 space-y-2">
          <label className="flex items-start gap-2 text-xs text-ink">
            <input
              type="checkbox"
              className="mt-0.5 accent-[rgb(var(--c-accent))]"
              checked={always}
              disabled={submitted}
              onChange={(e) => {
                setAlways(e.target.checked);
                setConfirmAlways(false);
              }}
            />
            <span>
              本会话内完全相同的命令不再询问。将加入白名单的内容：
              <code className="ml-1 break-all rounded bg-card px-1 font-mono">{lines[0]}</code>
            </span>
          </label>
          {always && (
            <label className="flex items-center gap-2 text-xs font-semibold text-bad">
              <input
                type="checkbox"
                checked={confirmAlways}
                disabled={submitted}
                onChange={(e) => setConfirmAlways(e.target.checked)}
              />
              我确认以后同样的命令会被直接执行
            </label>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              variant="secondary"
              disabled={submitted}
              onClick={() => setRejecting(true)}
              ref={rejectRef}
            >
              拒绝…
            </Button>
            <Button
              variant="primary"
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
