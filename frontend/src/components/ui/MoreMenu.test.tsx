import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MoreMenu } from './MoreMenu';

function setup() {
  const copy = vi.fn();
  const remove = vi.fn();
  render(
    <>
      <MoreMenu
        label="更多操作"
        items={[
          { label: '复制', onSelect: copy },
          { label: '不可用', onSelect: vi.fn(), disabled: true },
          { label: '删除', onSelect: remove, danger: true, separated: true },
        ]}
      />
      <button>外部</button>
    </>,
  );
  return { copy, remove, trigger: screen.getByRole('button', { name: '更多操作' }) };
}

describe('MoreMenu', () => {
  it('is closed until the trigger is clicked, then lists the actions', () => {
    const { trigger } = setup();
    expect(screen.queryByRole('menu')).toBeNull();
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    fireEvent.click(trigger);
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(screen.getAllByRole('menuitem').map((i) => i.textContent)).toEqual([
      '复制',
      '不可用',
      '删除',
    ]);
    expect(screen.getByRole('menuitem', { name: '不可用' })).toBeDisabled();
  });

  it('runs the chosen action and closes', () => {
    const { copy, remove, trigger } = setup();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('menuitem', { name: '删除' }));
    expect(remove).toHaveBeenCalledTimes(1);
    expect(copy).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('closes on Escape (focus back on the trigger) and on an outside press', () => {
    const { trigger } = setup();
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    fireEvent.click(trigger);
    fireEvent.pointerDown(screen.getByRole('button', { name: '外部' }));
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('moves between the enabled items with the arrow keys, skipping disabled ones', () => {
    const { trigger } = setup();
    fireEvent.click(trigger);
    const first = screen.getByRole('menuitem', { name: '复制' });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: '删除' }));
    fireEvent.keyDown(document.activeElement as Element, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(first);
  });
});
