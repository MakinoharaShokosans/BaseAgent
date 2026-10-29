import { useState, type ReactNode } from 'react';
import { Button, Modal } from '../ui/primitives';

/** 危险操作的二次确认。失败时留在对话框里显示原因（例如“有任务正在运行”），不静默关闭。 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  requireText,
  onConfirm,
  onClose,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  /** 要求输入这段文字才能确认（删除工作区等不可逆操作） */
  requireText?: string;
  onConfirm: () => Promise<unknown>;
  onClose: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [typed, setTyped] = useState('');
  const blocked = requireText !== undefined && typed.trim() !== requireText;

  const confirm = async () => {
    setPending(true);
    setError('');
    try {
      await onConfirm();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPending(false);
    }
  };

  return (
    <Modal title={title} onClose={onClose}>
      <div className="space-y-4 text-sm">
        <div className="leading-relaxed text-mute">{children}</div>
        {requireText !== undefined && (
          <input
            aria-label={`输入 ${requireText} 以确认`}
            className="field"
            placeholder={requireText}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
          />
        )}
        {error && (
          <p role="alert" className="text-bad">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>取消</Button>
          <Button variant="danger" disabled={pending || blocked} onClick={() => void confirm()}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
