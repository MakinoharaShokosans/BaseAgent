import { existsSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * 读取后端生成的访问令牌，由开发服务器的同源代理在**服务端侧**注入请求头。
 * 令牌因此不进入浏览器代码、不进入 URL。设计见 docs/09_api/03_access_and_errors.md §3.3。
 *
 * 数据目录与后端一致：环境变量 BASEAGENT_HOME（旧名 AEGIS_HOME 仍然有效），
 * 缺省 ~/.local/share/baseagent；改名前的 ~/.local/share/aegis 还在、新目录还没建时沿用旧目录。
 */
function defaultHome(): string {
  const share = path.join(os.homedir(), '.local', 'share');
  const next = path.join(share, 'baseagent');
  const legacy = path.join(share, 'aegis');
  return existsSync(legacy) && !existsSync(next) ? legacy : next;
}

function readApiToken(): string {
  const configured = process.env.BASEAGENT_HOME ?? process.env.AEGIS_HOME ?? defaultHome();
  const home = configured.replace(/^~(?=$|\/)/, os.homedir());
  try {
    return readFileSync(path.join(home, 'api_token'), 'utf8').trim();
  } catch {
    return ''; // 后端尚未生成令牌，或未启用认证
  }
}

const target =
  process.env.BASEAGENT_PROXY_TARGET ?? process.env.AEGIS_PROXY_TARGET ?? 'http://127.0.0.1:11328';

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
