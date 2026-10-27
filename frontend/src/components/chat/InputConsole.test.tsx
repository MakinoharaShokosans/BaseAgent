import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { InputConsole } from './InputConsole';

function type(text: string) {
  fireEvent.change(screen.getByLabelText('任务目标'), { target: { value: text } });
}

/** 自定义下拉：点开触发器，再点选项 */
function chooseLevel(label: string) {
  fireEvent.click(screen.getByRole('button', { name: '权限基线' }));
  fireEvent.click(screen.getByRole('option', { name: new RegExp(label) }));
}

const levelShown = () => screen.getByRole('button', { name: '权限基线' }).textContent;

describe('InputConsole', () => {
  it('submits with the default workspace_write baseline and no full-permission confirmation', async () => {
    const onSubmit = vi.fn();
    render(<InputConsole onSubmit={onSubmit} />);
    type('修一个 bug');
    fireEvent.click(screen.getByRole('button', { name: /发送/ }));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith('修一个 bug', 'workspace_write', false),
    );
    expect(screen.getByLabelText('任务目标')).toHaveValue('');
  });

  it('asks for explicit confirmation before sending full_permissions, and sends the flag', async () => {
    const onSubmit = vi.fn();
    render(<InputConsole onSubmit={onSubmit} />);
    type('部署');
    chooseLevel('完全权限');
    fireEvent.click(screen.getByRole('button', { name: /发送/ }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: '以完全权限运行？' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '我确认，以完全权限运行' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('部署', 'full_permissions', true));
  });

  it('cancelling the confirmation sends nothing, and full_permissions is not remembered', async () => {
    const onSubmit = vi.fn();
    render(<InputConsole onSubmit={onSubmit} />);
    type('部署');
    chooseLevel('完全权限');
    fireEvent.click(screen.getByRole('button', { name: /发送/ }));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(onSubmit).not.toHaveBeenCalled();
    chooseLevel('工作区读写');
    fireEvent.click(screen.getByRole('button', { name: /发送/ }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('部署', 'workspace_write', false));
    type('下一个');
    expect(levelShown()).toContain('工作区读写');
  });

  it('the permission dropdown is keyboard operable and shows each level hint', () => {
    render(<InputConsole onSubmit={vi.fn()} />);
    const trigger = screen.getByRole('button', { name: '权限基线' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    expect(screen.getByRole('listbox', { name: '权限基线' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /工作区读写/ })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByText(/不再请求确认/)).toBeInTheDocument();
    fireEvent.keyDown(trigger, { key: 'ArrowUp' });
    fireEvent.keyDown(trigger, { key: 'ArrowUp' });
    fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(levelShown()).toContain('完全权限');
    fireEvent.keyDown(trigger, { key: 'Enter' });
    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('disables sending for empty input or while a task is busy', () => {
    const { rerender } = render(<InputConsole onSubmit={vi.fn()} />);
    expect(screen.getByRole('button', { name: /发送/ })).toBeDisabled();
    type('   ');
    expect(screen.getByRole('button', { name: /发送/ })).toBeDisabled();
    type('go');
    expect(screen.getByRole('button', { name: /发送/ })).toBeEnabled();
    rerender(<InputConsole onSubmit={vi.fn()} disabled />);
    expect(screen.getByRole('button', { name: /发送/ })).toBeDisabled();
  });
});
