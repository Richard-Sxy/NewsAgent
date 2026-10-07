import assert from 'node:assert/strict'
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { createServer as createHttpServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import process from 'node:process'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { build, createServer, resolveConfig } from 'vite'

const frontendDirectory = fileURLToPath(new URL('..', import.meta.url))
const publicYamlToken = 'newsagent-vite-test-public-yaml-canary'
const publicOverrideToken = 'newsagent-vite-test-public-env-canary'
const publicPrivateCanary = 'newsagent-vite-test-public-private-prefix-canary'

function isolatedProject(t) {
  const rootDirectory = realpathSync(mkdtempSync(join(tmpdir(), 'newsagent-vite-config-test-')))
  const configFile = join(rootDirectory, 'vite.config.ts')
  const cleanup = []
  const savedEnvironment = new Map(
    Object.keys(process.env)
      .filter((key) => key.startsWith('VITE_') || key === 'NODE_ENV')
      .map((key) => [key, process.env[key]]),
  )
  for (const key of savedEnvironment.keys()) delete process.env[key]
  t.after(async () => {
    try {
      for (const close of cleanup.reverse()) await close()
    } finally {
      rmSync(rootDirectory, { recursive: true, force: true })
      for (const key of Object.keys(process.env)) {
        if (key.startsWith('VITE_') || key === 'NODE_ENV') delete process.env[key]
      }
      for (const [key, value] of savedEnvironment) process.env[key] = value
    }
  })

  mkdirSync(join(rootDirectory, 'dev-server'))
  mkdirSync(join(rootDirectory, 'src'))
  copyFileSync(join(frontendDirectory, 'vite.config.ts'), configFile)
  copyFileSync(
    join(frontendDirectory, 'dev-server', 'local-config.mjs'),
    join(rootDirectory, 'dev-server', 'local-config.mjs'),
  )
  const publicYaml = readFileSync(
    join(frontendDirectory, 'dev-server', 'local.yml'),
    'utf8',
  ).replace(/^ {2}token:.*$/m, `  token: ${publicYamlToken}`)
  writeFileSync(join(rootDirectory, 'dev-server', 'local.yml'), publicYaml, 'utf8')
  writeFileSync(join(rootDirectory, 'package.json'), '{"type":"module"}', 'utf8')
  writeFileSync(
    join(rootDirectory, 'index.html'),
    '<!doctype html><html><head><title>Public test probe</title></head><body><script type="module" src="/src/env-probe.js"></script></body></html>',
    'utf8',
  )
  writeFileSync(
    join(rootDirectory, 'src', 'env-probe.js'),
    'import { version } from "vue"; globalThis.newsAgentProbe = { version, environment: import.meta.env, simulation: import.meta.env.VITE_LOCAL_SIMULATION };',
    'utf8',
  )
  symlinkSync(join(frontendDirectory, 'node_modules'), join(rootDirectory, 'node_modules'), 'dir')
  return { rootDirectory, configFile, cleanup }
}

function inlineConfiguration({ rootDirectory, configFile }, overrides = {}) {
  return {
    root: rootDirectory,
    configFile,
    logLevel: 'silent',
    // No CSS is tested; prevent background discovery outside this fixture.
    css: { postcss: {} },
    ...overrides,
  }
}

function publicCanaryEnvironment() {
  process.env.VITE_DEV_GATEWAY_TOKEN = publicOverrideToken
  process.env.VITE_DEV_PRIVATE_CANARY = publicPrivateCanary
  process.env.VITE_LOCAL_SIMULATION = '1'
}

function apiProxy(config, preview = false) {
  return (preview ? config.preview.proxy : config.server.proxy)['/api']
}

function assertNoIdentity(config, preview = false) {
  assert.deepEqual(apiProxy(config, preview).headers ?? {}, {})
  assert.equal(config.define['import.meta.env.VITE_LOCAL_SIMULATION'], '"0"')
  assert.equal(config.env.VITE_LOCAL_SIMULATION, '0')
  assert.equal(
    Object.keys(config.env).some((key) => key.startsWith('VITE_DEV_')),
    false,
  )
}

test('the real Vite configuration loads the default YAML for development', async (t) => {
  const { rootDirectory, configFile } = isolatedProject(t)
  const config = await resolveConfig(inlineConfiguration({ rootDirectory, configFile }), 'serve')

  assert.equal(config.server.host, '127.0.0.1')
  assert.equal(config.server.port, 5174)
  assert.equal(apiProxy(config).target, 'http://127.0.0.1:28000')
  assert.equal(apiProxy(config).headers.Authorization === `Bearer ${publicYamlToken}`, true)
  assert.equal(config.define['import.meta.env.VITE_LOCAL_SIMULATION'], '"1"')
  assert.equal(config.env.VITE_LOCAL_SIMULATION, '1')
  assert.equal(
    Object.keys(config.env).some((key) => key.startsWith('VITE_DEV_')),
    false,
  )
})

test('resolved CLI host overrides cannot expose a proxy with injected identity', async (t) => {
  for (const host of ['0.0.0.0', true]) {
    await t.test(String(host), async (subtest) => {
      const { rootDirectory, configFile } = isolatedProject(subtest)
      await assert.rejects(() =>
        resolveConfig(
          inlineConfiguration({ rootDirectory, configFile }, { server: { host } }),
          'serve',
        ),
      )
    })
  }
})

test('old private dotenv settings cannot override YAML while explicit environment overrides still work', async (t) => {
  const project = isolatedProject(t)
  writeFileSync(
    join(project.rootDirectory, '.env.local'),
    [
      'VITE_DEV_PORT=5199',
      'VITE_DEV_PROXY_TARGET=http://127.0.0.1:29999',
      `VITE_DEV_GATEWAY_TOKEN=${publicPrivateCanary}`,
      'VITE_LOCAL_SIMULATION=0',
      'VITE_ROUTER_BASE=/public-dotenv-base/',
    ].join('\n'),
    'utf8',
  )
  const defaults = await resolveConfig(inlineConfiguration(project), 'serve')
  assert.equal(defaults.server.port, 5174)
  assert.equal(apiProxy(defaults).target, 'http://127.0.0.1:28000')
  assert.equal(apiProxy(defaults).headers.Authorization === `Bearer ${publicYamlToken}`, true)
  assert.equal(defaults.env.VITE_LOCAL_SIMULATION, '1')
  assert.equal(defaults.base, '/public-dotenv-base/')

  process.env.VITE_DEV_PORT = '5184'
  process.env.VITE_DEV_PROXY_TARGET = 'http://localhost:28100'
  process.env.VITE_DEV_GATEWAY_TOKEN = publicOverrideToken
  process.env.VITE_LOCAL_SIMULATION = '0'
  const overrides = await resolveConfig(inlineConfiguration(project), 'serve')
  assert.equal(overrides.server.port, 5184)
  assert.equal(apiProxy(overrides).target, 'http://localhost:28100')
  assert.equal(apiProxy(overrides).headers.Authorization === `Bearer ${publicOverrideToken}`, true)
  assert.equal(overrides.env.VITE_LOCAL_SIMULATION, '0')
})

test('real build and preview resolution omit local identity and simulation in every mode', async (t) => {
  const cases = [
    { command: 'build', mode: 'production', isPreview: false },
    { command: 'build', mode: 'development', isPreview: false },
    { command: 'serve', mode: 'production', isPreview: true },
    { command: 'serve', mode: 'development', isPreview: true },
  ]
  for (const { command, mode, isPreview } of cases) {
    await t.test(`${command}/${mode}/preview=${isPreview}`, async (subtest) => {
      const { rootDirectory, configFile } = isolatedProject(subtest)
      publicCanaryEnvironment()
      writeFileSync(join(rootDirectory, 'dev-server', 'local.yml'), 'gateway: [broken', 'utf8')
      const config = await resolveConfig(
        inlineConfiguration({ rootDirectory, configFile }, { mode }),
        command,
        mode,
        command === 'build' ? 'production' : 'development',
        isPreview,
      )
      assertNoIdentity(config, isPreview)
      assert.equal(apiProxy(config, isPreview).target, 'http://127.0.0.1:8000')
    })
  }
})

test('production and development builds never include local YAML or private environment canaries', async (t) => {
  for (const mode of ['production', 'development']) {
    await t.test(mode, async (subtest) => {
      const { rootDirectory, configFile } = isolatedProject(subtest)
      publicCanaryEnvironment()
      const result = await build(
        inlineConfiguration(
          { rootDirectory, configFile },
          {
            mode,
            build: { write: false, sourcemap: true },
          },
        ),
      )
      const bundles = Array.isArray(result) ? result : [result]
      const serializedOutputs = bundles.flatMap((bundle) =>
        bundle.output.map((output) => JSON.stringify(output)),
      )
      assert.equal(serializedOutputs.length > 0, true)
      assert.equal(
        bundles.some((bundle) =>
          bundle.output.some((output) => output.type === 'chunk' && output.map),
        ),
        true,
      )
      for (const output of serializedOutputs) {
        for (const canary of [
          publicYamlToken,
          publicOverrideToken,
          publicPrivateCanary,
          'VITE_DEV_',
        ]) {
          assert.equal(
            output.includes(canary),
            false,
            'Private development configuration reached a build output',
          )
        }
      }
      const entry = bundles
        .flatMap((bundle) => bundle.output)
        .find((output) => output.type === 'chunk' && output.isEntry)
      assert.equal(Boolean(entry), true)
      assert.equal(/VITE_LOCAL_SIMULATION\s*:\s*["']0["']/.test(entry.code), true)
      assert.equal(/simulation\s*:\s*["']0["']/.test(entry.code), true)
    })
  }
})

test(
  'the live development server hides Node configuration, filters browser env, and injects proxy headers',
  { timeout: 20000 },
  async (t) => {
    const { rootDirectory, configFile, cleanup } = isolatedProject(t)
    publicCanaryEnvironment()
    const echoServer = createHttpServer((request, response) => {
      response.setHeader('Content-Type', 'application/json')
      response.end(
        JSON.stringify({
          authorization: request.headers.authorization,
          tenant: request.headers['x-tenant-id'],
          user: request.headers['x-user-id'],
          hotNewsRoles: request.headers['x-hot-news-roles'],
          dataLoopRoles: request.headers['x-data-loop-roles'],
        }),
      )
    })
    await new Promise((resolve, reject) => {
      echoServer.once('error', reject)
      echoServer.listen(0, '127.0.0.1', resolve)
    })
    cleanup.push(
      () =>
        new Promise((resolve, reject) => {
          echoServer.closeAllConnections()
          echoServer.close((error) => (error ? reject(error) : resolve()))
        }),
    )
    process.env.VITE_DEV_PROXY_TARGET = `http://127.0.0.1:${echoServer.address().port}`
    const server = await createServer(
      inlineConfiguration(
        { rootDirectory, configFile },
        {
          mode: 'development',
          optimizeDeps: { noDiscovery: true, include: [] },
          server: { host: '127.0.0.1', port: 0, strictPort: false },
        },
      ),
    )
    cleanup.push(async () => {
      server.httpServer.closeAllConnections()
      await server.close()
    })
    await server.listen()
    const serverUrl = `http://127.0.0.1:${server.httpServer.address().port}`
    const absoluteYaml = join(rootDirectory, 'dev-server', 'local.yml')
    const protectedPaths = [
      '/dev-server/local.yml',
      '/dev-server/local.yml?raw',
      `/@fs${absoluteYaml}`,
      `/@fs${absoluteYaml}?raw`,
      `/@fs/${absoluteYaml}`,
      `/@fs/${absoluteYaml}?raw`,
      '/dev-server/local-config.mjs',
      '/dev-server/local-config.mjs?raw',
    ]
    for (const path of protectedPaths) {
      const response = await globalThis.fetch(`${serverUrl}${path}`, {
        signal: globalThis.AbortSignal.timeout(5000),
      })
      const body = await response.text()
      assert.equal(
        [403, 404].includes(response.status),
        true,
        'Node-only configuration was reachable by HTTP',
      )
      assert.equal(body.includes(publicYamlToken), false)
    }

    const browserResponse = await globalThis.fetch(`${serverUrl}/src/env-probe.js`, {
      signal: globalThis.AbortSignal.timeout(5000),
    })
    assert.equal(browserResponse.status, 200)
    const browserModule = await browserResponse.text()
    for (const canary of [publicYamlToken, publicOverrideToken, publicPrivateCanary, 'VITE_DEV_']) {
      assert.equal(
        browserModule.includes(canary),
        false,
        'Private development configuration reached a browser module',
      )
    }
    assert.equal(browserModule.includes('VITE_LOCAL_SIMULATION'), true)

    const proxyResponse = await globalThis.fetch(`${serverUrl}/api/public-header-probe`, {
      signal: globalThis.AbortSignal.timeout(5000),
    })
    assert.equal(proxyResponse.status, 200)
    const observedHeaders = await proxyResponse.json()
    const expectedHeaders = {
      authorization: `Bearer ${publicOverrideToken}`,
      tenant: '11111111-1111-4111-8111-111111111111',
      user: '22222222-2222-4222-8222-222222222222',
      hotNewsRoles: 'hot-news:admin',
      dataLoopRoles: 'data-loop:admin',
    }
    assert.equal(
      JSON.stringify(observedHeaders) === JSON.stringify(expectedHeaders),
      true,
      'Proxy header injection differs from public fixture',
    )
  },
)
