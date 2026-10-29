import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { Select } from './Select';
import { Slider } from './Slider';

const OPTIONS = [
  { value: 'a', label: 'Alpha' },
  { value: 'b', label: 'Beta', hint: '第二个' },
  { value: 'c', label: 'Gamma' },
] as const;

function ControlledSelect({ onChange }: { onChange?: (v: string) => void }) {
  const [value, setValue] = useState<string>('a');
  return (
    <>
      <Select
        label="字母"
        value={value}
        onChange={(v) => {
          setValue(v);
          onChange?.(v);
        }}
        options={[...OPTIONS]}
      />
      <button>外部</button>
    </>
  );
}

describe('Select', () => {
  it('opens on click, marks the selected option and closes after choosing', () => {
    const onChange = vi.fn();
    render(<ControlledSelect onChange={onChange} />);
    const trigger = screen.getByRole('button', { name: '字母' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'listbox');
    fireEvent.click(trigger);
    expect(screen.getByRole('option', { name: /Alpha/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('第二个')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('option', { name: /Beta/ }));
    expect(onChange).toHaveBeenCalledWith('b');
    expect(trigger).toHaveTextContent('Beta');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('closes on outside click and on Escape, returning focus to the trigger', () => {
    render(<ControlledSelect />);
    const trigger = screen.getByRole('button', { name: '字母' });
    fireEvent.click(trigger);
    fireEvent.mouseDown(screen.getByRole('button', { name: '外部' }));
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(document.activeElement).toBe(trigger);
  });

  it('wraps around with the arrow keys', () => {
    render(<ControlledSelect />);
    const trigger = screen.getByRole('button', { name: '字母' });
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    fireEvent.keyDown(trigger, { key: 'ArrowUp' });
    fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(trigger).toHaveTextContent('Gamma');
  });
});

describe('Slider', () => {
  function Controlled({ onChange }: { onChange: (v: number) => void }) {
    const [value, setValue] = useState(32000);
    return (
      <Slider
        label="上下文上限"
        value={value}
        min={4000}
        max={64000}
        step={1000}
        format={(n) => `${n / 1000}k`}
        onChange={(v) => {
          setValue(v);
          onChange(v);
        }}
      />
    );
  }

  it('exposes slider semantics and steps with the keyboard, clamped to the range', () => {
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const slider = screen.getByRole('slider', { name: '上下文上限' });
    expect(slider).toHaveAttribute('aria-valuenow', '32000');
    expect(slider).toHaveAttribute('aria-valuetext', '32k');
    fireEvent.keyDown(slider, { key: 'ArrowRight' });
    expect(onChange).toHaveBeenLastCalledWith(33000);
    fireEvent.keyDown(slider, { key: 'PageDown' });
    expect(onChange).toHaveBeenLastCalledWith(23000);
    fireEvent.keyDown(slider, { key: 'End' });
    expect(onChange).toHaveBeenLastCalledWith(64000);
    fireEvent.keyDown(slider, { key: 'ArrowRight' });
    expect(onChange).toHaveBeenLastCalledWith(64000);
    fireEvent.keyDown(slider, { key: 'Home' });
    expect(slider).toHaveAttribute('aria-valuenow', '4000');
  });

  it('log scale gives 4k–64k about half the track and steps by position', () => {
    function Log({ onChange }: { onChange: (v: number) => void }) {
      const [value, setValue] = useState(32000);
      return (
        <Slider
          label="上下文上限"
          scale="log"
          value={value}
          min={4000}
          max={1_000_000}
          step={1000}
          onChange={(v) => {
            setValue(v);
            onChange(v);
          }}
        />
      );
    }
    const onChange = vi.fn();
    const { container } = render(<Log onChange={onChange} />);
    const slider = screen.getByRole('slider', { name: '上下文上限' });
    // 32k 在对数刻度上约 38% 处，而线性刻度只有 2.8%
    expect(parseFloat(slider.style.left)).toBeCloseTo(37.66, 1);

    // 点在轨道正中：4k × 250^0.5 ≈ 63.2k，吸附到 63k
    const track = container.firstElementChild as HTMLElement;
    track.getBoundingClientRect = () =>
      ({ left: 0, width: 1000, top: 0, height: 24, right: 1000, bottom: 24 }) as DOMRect;
    fireEvent.pointerDown(track, { clientX: 500, pointerId: 1 });
    expect(onChange).toHaveBeenLastCalledWith(63000);

    fireEvent.keyDown(slider, { key: 'Home' });
    expect(onChange).toHaveBeenLastCalledWith(4000);
    // 低端 1% 不足一个 step：至少走一步
    fireEvent.keyDown(slider, { key: 'ArrowRight' });
    expect(onChange).toHaveBeenLastCalledWith(5000);
    fireEvent.keyDown(slider, { key: 'End' });
    fireEvent.keyDown(slider, { key: 'PageDown' });
    // 1M × 250^-0.1 ≈ 575.7k，吸附到 576k
    expect(onChange).toHaveBeenLastCalledWith(576000);
  });
});

describe('Checkbox', () => {
  it('is a custom checkbox with role semantics and toggles on click', async () => {
    const { Checkbox } = await import('./Checkbox');
    function Controlled() {
      const [on, setOn] = useState(false);
      return (
        <Checkbox label="本会话免审" checked={on} onChange={setOn}>
          本会话免审
        </Checkbox>
      );
    }
    render(<Controlled />);
    const box = screen.getByRole('checkbox', { name: '本会话免审' });
    expect(box).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(box);
    expect(box).toHaveAttribute('aria-checked', 'true');
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(box).toHaveAttribute('aria-checked', 'true');
  });

  it('does not toggle when disabled', async () => {
    const { Checkbox } = await import('./Checkbox');
    const onChange = vi.fn();
    render(
      <Checkbox label="禁用项" checked={false} disabled onChange={onChange}>
        禁用项
      </Checkbox>,
    );
    fireEvent.click(screen.getByRole('checkbox', { name: '禁用项' }));
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('Toggle', () => {
  it('uses aria-disabled instead of native disabled so the tooltip and focus still work', async () => {
    const { Toggle } = await import('./Toggle');
    const onChange = vi.fn();
    render(<Toggle label="MCP 总开关" checked tip="后端未提供" disabled onChange={onChange} />);
    const toggle = screen.getByRole('switch', { name: 'MCP 总开关' });
    expect(toggle).toHaveAttribute('aria-disabled', 'true');
    expect(toggle).not.toBeDisabled();
    fireEvent.click(toggle);
    expect(onChange).not.toHaveBeenCalled();
  });
});
