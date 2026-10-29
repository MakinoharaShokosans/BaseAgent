/** @type {import('tailwindcss').Config} */

// 颜色全部来自 CSS 变量（RGB 三元组），明暗主题只切换变量。设计见 docs/10_frontend/03_visual_design.md §3。
const token = (name) => `rgb(var(--c-${name}) / <alpha-value>)`;

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: ['class', '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        canvas: token('canvas'),
        side: token('side'),
        card: token('card'),
        raised: token('raised'),
        line: token('line'),
        ink: token('ink'),
        mute: token('mute'),
        accent: token('accent'),
        'accent-soft': token('accent-soft'),
        'accent-2': token('accent-2'),
        ok: token('ok'),
        warn: token('warn'),
        bad: token('bad'),
        code: token('code'),
        'code-ink': token('code-ink'),
      },
      fontFamily: {
        sans: [
          '"Geist Variable"',
          '"HarmonyOS Sans SC"',
          '"MiSans"',
          '"PingFang SC"',
          '"Noto Sans CJK SC"',
          '"Microsoft YaHei"',
          'system-ui',
          'sans-serif',
        ],
        display: ['"Instrument Serif"', '"Noto Serif CJK SC"', '"Songti SC"', 'serif'],
        mono: ['"Geist Mono Variable"', '"JetBrains Mono"', 'ui-monospace', 'Menlo', 'monospace'],
      },
      fontSize: { '2xs': ['11px', '16px'] },
      opacity: { 12: '0.12' },
      // 阶梯圆角与同心嵌套标尺：micro(6px), sm(8px), base(12px), md(14px), lg(18px), bento(22px), large(24px), full
      borderRadius: {
        micro: '6px',
        sm: '8px',
        base: '12px',
        md: '14px',
        lg: '18px',
        bento: '22px',
        large: '24px',
        xl2: '1.125rem',
        xl3: '1.5rem',
        pill: '9999px',
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        float: 'var(--shadow-float)',
        bento: 'var(--shadow-bento)',
        glow: 'var(--shadow-glow)',
      },
      transitionTimingFunction: {
        out: 'cubic-bezier(.22,1,.36,1)',
        'in-out': 'cubic-bezier(.65,0,.35,1)',
        spring: 'cubic-bezier(.175,.885,.32,1.15)',
      },
      keyframes: {
        sheen: {
          '0%': { transform: 'translateX(-120%)' },
          '100%': { transform: 'translateX(220%)' },
        },
        breathe: {
          '0%,100%': { opacity: '0.5', transform: 'scale(1)' },
          '50%': { opacity: '0.9', transform: 'scale(1.05)' },
        },
        halo: {
          '0%': { boxShadow: '0 0 0 0 rgb(var(--c-accent) / 0.45)' },
          '100%': { boxShadow: '0 0 0 6px rgb(var(--c-accent) / 0)' },
        },
        dots: { '0%,80%,100%': { opacity: '0.25' }, '40%': { opacity: '1' } },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
      animation: {
        sheen: 'sheen 1.8s cubic-bezier(.65,0,.35,1) infinite',
        breathe: 'breathe 7s ease-in-out infinite',
        halo: 'halo 2.2s ease-out infinite',
        dots: 'dots 1.2s ease-in-out infinite',
        shimmer: 'shimmer 2.5s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};
