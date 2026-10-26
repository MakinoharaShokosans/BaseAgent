import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * 读取后端生成的访问令牌，由开发服务器的同源代理在**服务端侧**注入请求头。
 * 令牌因此不进入浏览器代码、不进入 URL。设计见 docs/09_api/03_access_and_errors.md §3.3。
 *
 * 数据目录与后端一致：环境变量 AEGIS_HOME，缺省 ~/.local/share/aegis。
 */
function readApiToken(): string {
  const fallback = path.join(os.homedir(), '.local', 'share', 'aegis');
  const home = (process.env.AEGIS_HOME ?? fallback).replace(/^~(?=$|\/)/, os.homedir());
  try {
    return readFileSync(path.join(home, 'api_token'), 'utf8').trim();
  } catch {
    return ''; // 后端尚未生成令牌，或未启用认证
  }
}

const target = process.env.AEGIS_PROXY_TARGET ?? 'http://127.0.0.1:8000';

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    host: '127.0.0.1', // 只监听回环地址
    port: 5173,
    proxy: {
      '/api': {
        target,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            const token = readApiToken(); // 每次请求读取，令牌轮换后无需重启前端
            if (token) proxyReq.setHeader('Authorization', `Bearer ${token}`);
          });
        },
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    exclude: ['e2e/**', 'node_modules/**'],
  },
});
