import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'
import type { ProxyOptions } from 'vite'
import { assertLocalDevBoundary, loadLocalDevSettings } from './dev-server/local-config.mjs'

/**
 * 构建期只决定"资源怎么打"，不决定"请求打到哪"。
 * 运行期地址由 public/config.json 注入，保证同一镜像可跨环境发布。
 */
export default defineConfig(({ command, mode, isPreview }) => {
  const rootDirectory = fileURLToPath(new URL('.', import.meta.url))
  const env = loadEnv(mode, rootDirectory, 'VITE_')
  if (command === 'serve' && mode === 'development' && !isPreview) {
    // YAML replaces legacy .env dev settings; explicit shell overrides still work.
    for (const key of Object.keys(env)) {
      if (
        (key.startsWith('VITE_DEV_') || key === 'VITE_LOCAL_SIMULATION') &&
        !Object.hasOwn(process.env, key)
      ) {
        delete env[key]
      }
    }
  }
  const settings = loadLocalDevSettings({
    rootDirectory,
    command,
    mode,
    isPreview,
    env,
  })

  /**
   * 本地开发时没有企业网关注入身份头，而生产环境**必须**由网关注入。
   * 为保持"前端代码永不设置身份头"这条不变式，身份头只在开发代理这一层补，
   * 绝不进入 src/ 下的任何一行代码。生产构建不会带上这里的任何值。
   */
  const devIdentityHeaders = settings.identityHeaders
  const hasDevIdentity = Object.keys(devIdentityHeaders).length > 0

  const apiProxy: ProxyOptions = {
    target: settings.proxyTarget,
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
    root: rootDirectory,
    base: settings.routerBase,
    // Private VITE_DEV_* overrides remain in Node, never import.meta.env.
    envPrefix: ['VITE_APP_TITLE', 'VITE_ROUTER_BASE', 'VITE_LOCAL_SIMULATION'],
    define: {
      'import.meta.env.VITE_LOCAL_SIMULATION': JSON.stringify(settings.localSimulation ? '1' : '0'),
    },
    plugins: [
      vue(),
      {
        name: 'newsagent-local-identity-boundary',
        configResolved(resolved) {
          // Whole import.meta.env must agree with the statically replaced flag.
          resolved.env.VITE_LOCAL_SIMULATION = settings.localSimulation ? '1' : '0'
          assertLocalDevBoundary({
            host: resolved.server.host,
            proxyTarget: settings.proxyTarget,
            identityHeaders: devIdentityHeaders,
          })
        },
      },
    ],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      host: settings.host,
      port: settings.port,
      strictPort: true,
      proxy: { '/api': apiProxy },
      fs: {
        // Retain Vite's default secret-file denials and hide Node-only config.
        deny: ['.env', '.env.*', '*.{crt,pem}', '**/.git/**', '**/dev-server/**'],
      },
    },
    preview: {
      port: 4173,
      // Production preview has no local YAML identity; use a real gateway.
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
