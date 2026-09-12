import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'
import type { ProxyOptions } from 'vite'

/**
 * 构建期只决定"资源怎么打"，不决定"请求打到哪"。
 * 运行期地址由 public/config.json 注入，保证同一镜像可跨环境发布。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const devProxyTarget = env.VITE_DEV_PROXY_TARGET || 'http://127.0.0.1:8000'
  // 端口保持团队默认 5173，本地可用 .env.*.local 的 VITE_DEV_PORT 覆盖。
  const devPort = Number(env.VITE_DEV_PORT) || 5173

  /**
   * 本地开发时没有企业网关注入身份头，而生产环境**必须**由网关注入。
   * 为保持"前端代码永不设置身份头"这条不变式，身份头只在开发代理这一层补，
   * 绝不进入 src/ 下的任何一行代码。生产构建不会带上这里的任何值。
   */
  const devIdentityHeaders: Record<string, string> = {}
  if (env.VITE_DEV_TENANT_ID) devIdentityHeaders['X-Tenant-ID'] = env.VITE_DEV_TENANT_ID
  if (env.VITE_DEV_USER_ID) devIdentityHeaders['X-User-ID'] = env.VITE_DEV_USER_ID
  if (env.VITE_DEV_DATA_LOOP_ROLES) {
    devIdentityHeaders['X-Data-Loop-Roles'] = env.VITE_DEV_DATA_LOOP_ROLES
  }
  // 热点控制台与 Data Loop 复用同一个网关共享 Token，但角色头独立。
  if (env.VITE_DEV_HOT_NEWS_ROLES) {
    devIdentityHeaders['X-Hot-News-Roles'] = env.VITE_DEV_HOT_NEWS_ROLES
  }
  if (env.VITE_DEV_GATEWAY_TOKEN) {
    devIdentityHeaders.Authorization = `Bearer ${env.VITE_DEV_GATEWAY_TOKEN}`
  }
  const hasDevIdentity = Object.keys(devIdentityHeaders).length > 0

  const apiProxy: ProxyOptions = {
    target: devProxyTarget,
    changeOrigin: true,
    ...(hasDevIdentity ? { headers: devIdentityHeaders } : {}),
    // SSE 必须关闭代理侧缓冲，否则事件流会被积压。
    configure: (proxy) => {
      proxy.on('proxyRes', (proxyRes) => {
        if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
          proxyRes.headers['x-accel-buffering'] = 'no'
        }
      })
    },
  }

  return {
    base: env.VITE_ROUTER_BASE || '/',
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: devPort,
      strictPort: true,
      proxy: { '/api': apiProxy },
    },
    preview: {
      port: 4173,
      // 让生产构建产物也能直接对着真实后端验证，不必依赖 dev server。
      proxy: { '/api': apiProxy },
    },
    build: {
      target: 'es2022',
      sourcemap: true,
      chunkSizeWarningLimit: 800,
      rollupOptions: {
        output: {
          manualChunks: {
            'vendor-vue': ['vue', 'vue-router', 'pinia'],
          },
          entryFileNames: 'assets/[name]-[hash].js',
          chunkFileNames: 'assets/[name]-[hash].js',
          assetFileNames: 'assets/[name]-[hash][extname]',
        },
      },
    },
  }
})
