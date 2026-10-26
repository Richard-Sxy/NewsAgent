// Isolated workbench build: no dotenv loading and no enterprise gateway identity.
import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: '/workbench/',
  envFile: false,
  define: { 'import.meta.env.VITE_LOCAL_SIMULATION': '"0"' },
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: {
    outDir: '../../var/streaming-lab/ui',
    emptyOutDir: false,
    target: 'es2022',
    sourcemap: false,
  },
})
