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
    expect(screen.getByRole('heading', { name: 'Aegis' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/还没有工作区/)).toBeInTheDocument());
    expect(screen.getByText('在左侧选择一个会话，或新建一个会话开始')).toBeInTheDocument();
    const calls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u === '/api/v1/workspaces')).toBe(true);
    expect(calls.every((u) => !u.includes('token='))).toBe(true);
  });
});
