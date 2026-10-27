/**
 * 渲染来自 Agent 的文本：一律视为不可信。
 * 不启用原始 HTML（不用 rehype-raw），不用 dangerouslySetInnerHTML；链接只允许 http/https/mailto，
 * 外链带 rel="noopener noreferrer"；图片不自动加载（外部图片可被用来外带数据）。
 */
import { Check, Copy } from 'lucide-react';
import { isValidElement, useRef, useState, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import remarkGfm from 'remark-gfm';
import { safeHref, stripControl } from '../../utils/text';

/** 代码块：顶部一条显示语言与"复制"按钮 */
function CodeBlock({ children }: { children?: ReactNode }) {
  const ref = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  const className =
    isValidElement<{ className?: string }>(children) && typeof children.props.className === 'string'
      ? children.props.className
      : '';
  const language = /language-([\w+#-]+)/.exec(className)?.[1] ?? '';
  return (
    <div className="not-prose overflow-hidden rounded-2xl bg-code text-code-ink">
      <div className="flex items-center px-4 pt-2.5 text-2xs text-code-ink/50">
        <span className="font-mono">{language || 'text'}</span>
        <button
          className="ml-auto flex items-center gap-1 rounded-md px-1.5 py-0.5 transition-colors hover:bg-white/10 hover:text-code-ink"
          aria-label="复制代码"
          onClick={() => {
            void navigator.clipboard?.writeText(ref.current?.textContent ?? '');
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          }}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre ref={ref}>{children}</pre>
    </div>
  );
}

export function SafeMarkdown({ text }: { text: string }) {
  return (
    <div className="prose-agent">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: false }]]}
        skipHtml
        urlTransform={(url) => safeHref(url) ?? ''}
        components={{
          a: ({ href, children }) => {
            const safe = safeHref(href);
            return safe ? (
              <a
                href={safe}
                target="_blank"
                rel="noopener noreferrer"
                className="text-accent underline decoration-accent/30 underline-offset-[3px] hover:decoration-accent"
              >
                {children}
              </a>
            ) : (
              <span>{children}</span>
            );
          },
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          img: ({ alt }) => <span className="text-mute">[图片已拦截{alt ? `：${alt}` : ''}]</span>,
        }}
      >
        {stripControl(text)}
      </ReactMarkdown>
    </div>
  );
}
