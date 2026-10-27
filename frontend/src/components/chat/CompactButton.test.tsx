import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/endpoints';
import { useAppStore } from '../../stores/useAppStore';
import type { CompactionResult } from '../../types/api';
import { CompactButton } from './CompactButton';

const memory = {
  summary: 's',
  watermark: 4,
  facts: [],
  failures: [],
  pending_facts: [],
  pending_failures: [],
};

function mount(disabled = false) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const spy = vi.spyOn(client, 'invalidateQueries');
  render(
    <QueryClientProvider client={client}>
      <CompactButton sessionId="s1" disabled={disabled} />
    </QueryClientProvider>,
  );
  return spy;
}

const click = () => fireEvent.click(screen.getByRole('button', { name: '压缩记忆' }));

describe('CompactButton', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useAppStore.setState({ notice: null });
  });

  it('compacts the session, reports how many turns were folded and refreshes the memory view', async () => {
    const result: CompactionResult = { status: 'compacted', turns: 4, memory };
    const compact = vi.spyOn(api.sessions, 'compact').mockResolvedValue(result);
    const invalidate = mount();
    click();
    expect(await screen.findByText('已压缩 4 条')).toBeInTheDocument();
    expect(compact).toHaveBeenCalledWith('s1');
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['smemory', 's1'] });
  });

  it('says so when there is nothing to compact', async () => {
    vi.spyOn(api.sessions, 'compact').mockResolvedValue({
      status: 'nothing_to_compact',
      turns: 0,
      memory,
    });
    mount();
    click();
    expect(await screen.findByText('没有可压缩的内容')).toBeInTheDocument();
  });

  it('shows progress and blocks a second click while the request is in flight', async () => {
    let finish!: (r: CompactionResult) => void;
    const compact = vi
      .spyOn(api.sessions, 'compact')
      .mockReturnValue(new Promise<CompactionResult>((resolve) => (finish = resolve)));
    mount();
    click();
    expect(await screen.findByText('压缩中…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '压缩记忆' })).toBeDisabled();
    click();
    expect(compact).toHaveBeenCalledTimes(1);
    finish({ status: 'compacted', turns: 2, memory });
    expect(await screen.findByText('已压缩 2 条')).toBeInTheDocument();
  });

  it('surfaces a failure as a notice and leaves the button usable', async () => {
    vi.spyOn(api.sessions, 'compact').mockRejectedValue(
      Object.assign(new Error('压缩失败，记忆保持不变，稍后再试'), { traceId: 'tr-1' }),
    );
    mount();
    click();
    await waitFor(() =>
      expect(useAppStore.getState().notice).toEqual({
        message: '压缩失败，记忆保持不变，稍后再试',
        traceId: 'tr-1',
      }),
    );
    expect(screen.getByRole('button', { name: '压缩记忆' })).toBeEnabled();
  });

  it('is disabled while a task is running', () => {
    mount(true);
    expect(screen.getByRole('button', { name: '压缩记忆' })).toBeDisabled();
  });
});
