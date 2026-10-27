import { useState, type ReactNode } from 'react';
import { Button, Modal } from '../ui/primitives';

/** 危险操作的二次确认。失败时留在对话框里显示原因（例如“有任务正在运行”），不静默关闭。 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  onConfirm,
  onClose,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => Promise<unknown>;
  onClose: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');

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
        {error && (
          <p role="alert" className="text-bad">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>取消</Button>
          <Button variant="danger" disabled={pending} onClick={() => void confirm()}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
