import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import {
  computeTimelineWindow,
  ConversationTimelineRail,
  type DialogueNode,
} from './ConversationTimelineRail';

describe('computeTimelineWindow', () => {
  it('returns all nodes when total <= windowSize', () => {
    const res = computeTimelineWindow(5, 2, 10);
    expect(res).toEqual({
      start: 0,
      end: 4,
      hasTopEllipsis: false,
      hasBottomEllipsis: false,
    });
  });

  it('handles exactly windowSize nodes without ellipsis', () => {
    const res = computeTimelineWindow(10, 5, 10);
    expect(res).toEqual({
      start: 0,
      end: 9,
      hasTopEllipsis: false,
      hasBottomEllipsis: false,
    });
  });

  it('positions at the beginning of a long list with bottom ellipsis only', () => {
    const res = computeTimelineWindow(20, 1, 10);
    expect(res).toEqual({
      start: 0,
      end: 9,
      hasTopEllipsis: false,
      hasBottomEllipsis: true,
    });
  });

  it('centers around activeIndex in the middle of a long list with both ellipses', () => {
    const res = computeTimelineWindow(30, 15, 10);
    expect(res.start).toBe(10);
    expect(res.end).toBe(19);
    expect(res.hasTopEllipsis).toBe(true);
    expect(res.hasBottomEllipsis).toBe(true);
  });

  it('positions at the end of a long list with top ellipsis only', () => {
    const res = computeTimelineWindow(25, 24, 10);
    expect(res).toEqual({
      start: 15,
      end: 24,
      hasTopEllipsis: true,
      hasBottomEllipsis: false,
    });
  });
});

describe('ConversationTimelineRail Component', () => {
  const sampleDialogues: DialogueNode[] = Array.from({ length: 15 }, (_, i) => ({
    id: `task-${i}`,
    goal: `任务目标第 ${i + 1} 步测试`,
    index: i,
  }));

  it('renders null when total dialogues < 2', () => {
    const { container } = render(
      <ConversationTimelineRail
        dialogues={[{ id: '1', goal: 'single', index: 0 }]}
        activeIndex={0}
        onSelect={() => {}}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders 10 nodes and bottom ellipsis when activeIndex is 0', () => {
    render(
      <ConversationTimelineRail
        dialogues={sampleDialogues}
        activeIndex={0}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByRole('navigation', { name: '对话时间轴导航' })).toBeInTheDocument();
    expect(screen.getByLabelText('跳转至更新对话')).toBeInTheDocument();
    expect(screen.queryByLabelText('跳转至更早对话')).not.toBeInTheDocument();
  });

  it('triggers onSelect when clicking a node button', () => {
    const handleSelect = vi.fn();
    render(
      <ConversationTimelineRail
        dialogues={sampleDialogues}
        activeIndex={3}
        onSelect={handleSelect}
      />,
    );
    const node = screen.getByLabelText(/第 2 轮对话/);
    fireEvent.click(node);
    expect(handleSelect).toHaveBeenCalledWith(1);
  });
});
