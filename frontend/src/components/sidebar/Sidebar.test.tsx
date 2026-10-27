import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAppStore } from '../../stores/useAppStore';
import { Sidebar } from './Sidebar';

const WORKSPACE = {
  workspace_id: 'w1',
  name: '示例项目',
  root_path: '/home/me/demo',
  description: '',
  created_at: 1,
};
const SESSIONS = [
  { session_id: 's2', workspace_id: 'w1', created_at: 2 },
  { session_id: 's1', workspace_id: 'w1', created_at: 1 },
];

let calls: { method: string; url: string; body?: string }[] = [];

function respond(url: string, method: string): Response {
  const json = (body: unknown, status = 200) =>
    new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
  if (method === 'POST' && url.endsWith('/sessions/s2/fork'))
    return json({ session_id: 's9', workspace_id: 'w1', created_at: 3 }, 201);
  if (method === 'DELETE') return json(null, 204);
  if (method === 'PATCH') return json({ ...WORKSPACE, name: '新名字' });
  if (url.endsWith('/workspaces')) return json([WORKSPACE]);
  if (url.endsWith('/workspaces/w1/sessions')) return json(SESSIONS);
  return json({});
}

function renderSidebar() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Sidebar />
    </QueryClientProvider>,
  );
}

describe('Sidebar more-actions menus', () => {
  beforeEach(() => {
    calls = [];
    useAppStore.setState({ workspaceId: 'w1', sessionId: 's2', run: null, notice: null });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET';
        calls.push({ method, url: String(url), body: init?.body as string | undefined });
        return respond(String(url), method);
      }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it('duplicates a session and switches to the copy', async () => {
    renderSidebar();
    fireEvent.click(await screen.findByRole('button', { name: '会话 2 的更多操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '创建副本' }));
    await waitFor(() => expect(useAppStore.getState().sessionId).toBe('s9'));
    expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/sessions/s2/fork'))).toBe(
      true,
    );
    expect(useAppStore.getState().notice?.message).toBe('已创建副本');
  });

  it('asks before deleting a session, then deletes it and clears the selection', async () => {
    renderSidebar();
    fireEvent.click(await screen.findByRole('button', { name: '会话 2 的更多操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '删除会话' }));
    expect(screen.getByRole('dialog', { name: '删除会话' })).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    await waitFor(() => expect(useAppStore.getState().sessionId).toBeNull());
    expect(calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/sessions/s2'))).toBe(true);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('keeps the dialog open and shows the reason when the backend refuses the delete', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) =>
        init?.method === 'DELETE'
          ? new Response(
              JSON.stringify({
                error: { code: 'TASK_STATE_CONFLICT', message: '有任务正在运行，先取消再删除' },
              }),
              { status: 409, headers: { 'content-type': 'application/json' } },
            )
          : respond(String(url), init?.method ?? 'GET'),
      ),
    );
    renderSidebar();
    fireEvent.click(await screen.findByRole('button', { name: '会话 2 的更多操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '删除会话' }));
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('有任务正在运行');
    expect(screen.getByRole('dialog', { name: '删除会话' })).toBeInTheDocument();
    expect(useAppStore.getState().sessionId).toBe('s2');
  });

  it('renames a workspace through the menu', async () => {
    renderSidebar();
    fireEvent.click(await screen.findByRole('button', { name: '示例项目 的更多操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '重命名' }));
    fireEvent.change(screen.getByRole('textbox', { name: '名称' }), {
      target: { value: '新名字' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'PATCH')?.body).toBe(
        JSON.stringify({ name: '新名字' }),
      ),
    );
  });

  it('warns that removing a workspace deletes its sessions but not the files', async () => {
    renderSidebar();
    fireEvent.click(await screen.findByRole('button', { name: '示例项目 的更多操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '移除工作区' }));
    const dialog = screen.getByRole('dialog', { name: '移除工作区' });
    expect(dialog).toHaveTextContent('磁盘上的项目文件不会被删除');
    fireEvent.click(screen.getByRole('button', { name: '移除' }));
    await waitFor(() => expect(useAppStore.getState().workspaceId).toBeNull());
    expect(calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/workspaces/w1'))).toBe(true);
  });

  it('copies the workspace path', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    renderSidebar();
    fireEvent.click(await screen.findByRole('button', { name: '示例项目 的更多操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '复制路径' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('/home/me/demo'));
    expect(useAppStore.getState().notice?.message).toBe('路径已复制');
  });
});
