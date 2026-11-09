/** Explicit local launcher: YAML + shell overrides, never dotenv files. */
import { fileURLToPath } from 'node:url'
import process from 'node:process'
import { build, createServer } from 'vite'

const action = process.argv[2]
if (!['serve', 'build'].includes(action) || process.argv.length !== 3) {
  throw new Error('Use node dev-server/isolated.mjs serve|build')
}
process.env.NEWSAGENT_SKIP_DOTENV = '1'
const options = {
  configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
  envFile: false,
}
if (action === 'build') {
  await build(options)
} else {
  const server = await createServer(options)
  await server.listen()
  server.printUrls()
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.once(signal, async () => {
      await server.close()
      process.exit(0)
    })
  }
}
