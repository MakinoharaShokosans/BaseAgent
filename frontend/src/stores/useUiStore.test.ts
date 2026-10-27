import { beforeEach, describe, expect, it, vi } from 'vitest';

async function freshStore() {
  vi.resetModules();
  return (await import('./useUiStore')).useUiStore;
}

describe('useUiStore persistence', () => {
  beforeEach(() => window.localStorage.clear());

  it('picks up preferences saved under the pre-rename key', async () => {
    window.localStorage.setItem(
      'aegis.ui.v2',
      JSON.stringify({ theme: 'dark', inspectorOpen: false }),
    );
    const store = await freshStore();
    expect(store.getState().theme).toBe('dark');
    expect(store.getState().inspectorOpen).toBe(false);
  });

  it('prefers the new key and writes new changes there', async () => {
    window.localStorage.setItem('aegis.ui.v2', JSON.stringify({ theme: 'dark' }));
    window.localStorage.setItem('baseagent.ui.v2', JSON.stringify({ theme: 'light' }));
    const store = await freshStore();
    expect(store.getState().theme).toBe('light');
    store.getState().toggleInspector();
    expect(JSON.parse(window.localStorage.getItem('baseagent.ui.v2') ?? '{}').inspectorOpen).toBe(
      false,
    );
  });
});
