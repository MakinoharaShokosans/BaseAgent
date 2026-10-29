import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';

describe('App', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders the shell and asks the backend for workspaces', async () => {
    const fetchMock = vi.fn(
      async (url: string) =>
        new Response(JSON.stringify(String(url).includes('/workspaces') ? [] : {}), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    expect(await screen.findByLabelText('侧栏')).toBeInTheDocument();
    // 一个工作区都没有：侧栏与主区都只留添加动作（空状态不放文案）
    await waitFor(() =>
      expect(screen.getAllByRole('button', { name: '添加工作区' }).length).toBeGreaterThan(0),
    );
    const calls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u === '/api/v1/workspaces')).toBe(true);
    expect(calls.every((u) => !u.includes('token='))).toBe(true);
  });
});
