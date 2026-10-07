import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import {
  assertLocalDevBoundary,
  loadLocalDevSettings,
  parseLocalDevYaml,
} from '../dev-server/local-config.mjs'

const tenantId = '11111111-1111-4111-8111-111111111111'
const userId = '22222222-2222-4222-8222-222222222222'
const publicToken = 'newsagent-test-public-local-token'
const fixtureYaml = `schema_version: 1
server:
  host: '127.0.0.1'
  port: 5174
proxy:
  target: 'http://127.0.0.1:28000'
simulation:
  enabled: true
gateway:
  enabled: true
  tenant_id: '${tenantId}'
  user_id: '${userId}'
  hot_news_roles: ['hot-news:admin']
  data_loop_roles: ['data-loop:admin']
  token: '${publicToken}'
`

function fixtureRoot(t, source = fixtureYaml) {
  const rootDirectory = mkdtempSync(join(tmpdir(), 'newsagent-frontend-config-test-'))
  t.after(() => rmSync(rootDirectory, { recursive: true, force: true }))
  if (source !== null) {
    mkdirSync(join(rootDirectory, 'dev-server'))
    writeFileSync(join(rootDirectory, 'dev-server', 'local.yml'), source, 'utf8')
  }
  return rootDirectory
}

function loadDevelopment(rootDirectory, env = {}) {
  return loadLocalDevSettings({
    rootDirectory,
    command: 'serve',
    mode: 'development',
    env,
  })
}

test('development loads the YAML into server settings and proxy identity headers', (t) => {
  const rootDirectory = fixtureRoot(t)
  const settings = loadDevelopment(rootDirectory)

  assert.equal(settings.host, '127.0.0.1')
  assert.equal(settings.port, 5174)
  assert.equal(settings.proxyTarget, 'http://127.0.0.1:28000')
  assert.equal(settings.localSimulation, true)
  assert.equal(settings.routerBase, '/')
  assert.equal(settings.configPath, join(rootDirectory, 'dev-server', 'local.yml'))
  assert.deepEqual(settings.identityHeaders, {
    'X-Tenant-ID': tenantId,
    'X-User-ID': userId,
    'X-Hot-News-Roles': 'hot-news:admin',
    'X-Data-Loop-Roles': 'data-loop:admin',
    Authorization: `Bearer ${publicToken}`,
  })
})

test('environment fields override YAML settings without mutating the supplied environment', (t) => {
  const rootDirectory = fixtureRoot(t)
  const env = Object.freeze({
    VITE_DEV_PORT: '5184',
    VITE_DEV_PROXY_TARGET: 'http://localhost:28100',
    VITE_DEV_TENANT_ID: '33333333-3333-4333-8333-333333333333',
    VITE_DEV_USER_ID: '44444444-4444-4444-8444-444444444444',
    VITE_DEV_HOT_NEWS_ROLES: 'hot-news:admin',
    VITE_DEV_DATA_LOOP_ROLES: 'data-loop:admin',
    VITE_DEV_GATEWAY_TOKEN: 'newsagent-test-public-override-token',
    VITE_LOCAL_SIMULATION: '0',
    VITE_ROUTER_BASE: '/console/',
  })
  const settings = loadDevelopment(rootDirectory, env)

  assert.equal(settings.port, 5184)
  assert.equal(settings.proxyTarget, env.VITE_DEV_PROXY_TARGET)
  assert.equal(settings.localSimulation, false)
  assert.equal(settings.routerBase, '/console/')
  assert.deepEqual(settings.identityHeaders, {
    'X-Tenant-ID': env.VITE_DEV_TENANT_ID,
    'X-User-ID': env.VITE_DEV_USER_ID,
    'X-Hot-News-Roles': env.VITE_DEV_HOT_NEWS_ROLES,
    'X-Data-Loop-Roles': env.VITE_DEV_DATA_LOOP_ROLES,
    Authorization: `Bearer ${env.VITE_DEV_GATEWAY_TOKEN}`,
  })
})

test('empty environment identity fields explicitly remove the corresponding YAML headers', async (t) => {
  const fields = [
    ['VITE_DEV_TENANT_ID', 'X-Tenant-ID'],
    ['VITE_DEV_USER_ID', 'X-User-ID'],
    ['VITE_DEV_HOT_NEWS_ROLES', 'X-Hot-News-Roles'],
    ['VITE_DEV_DATA_LOOP_ROLES', 'X-Data-Loop-Roles'],
    ['VITE_DEV_GATEWAY_TOKEN', 'Authorization'],
  ]
  for (const [envKey, headerKey] of fields) {
    await t.test(envKey, (subtest) => {
      const rootDirectory = fixtureRoot(subtest)
      const settings = loadDevelopment(rootDirectory, { [envKey]: '' })
      assert.equal(Object.hasOwn(settings.identityHeaders, headerKey), false)
      assert.equal(Object.keys(settings.identityHeaders).length, 4)
    })
  }
})

test('the gateway switch disables every identity header', async (t) => {
  await t.test('YAML disabled', (subtest) => {
    const rootDirectory = fixtureRoot(
      subtest,
      fixtureYaml.replace('gateway:\n  enabled: true', 'gateway:\n  enabled: false'),
    )
    assert.deepEqual(loadDevelopment(rootDirectory).identityHeaders, {})
  })
  await t.test('environment disabled', (subtest) => {
    const rootDirectory = fixtureRoot(subtest)
    assert.deepEqual(
      loadDevelopment(rootDirectory, { VITE_DEV_GATEWAY_ENABLED: '0' }).identityHeaders,
      {},
    )
  })
})

test('approved role arrays and comma-separated environment roles retain the existing header format', (t) => {
  const rootDirectory = fixtureRoot(t)
  const hotNewsRoles = ['hot-news:read', 'hot-news:decide', 'hot-news:handoff', 'hot-news:admin']
  const dataLoopRoles = [
    'data-loop:read',
    'data-loop:feedback-write',
    'data-loop:label-submit',
    'data-loop:label-approve',
    'data-loop:dataset-manage',
    'data-loop:candidate-manage',
    'data-loop:run',
    'data-loop:release-approve',
    'data-loop:rollback',
    'data-loop:admin',
  ]
  assert.doesNotThrow(() =>
    parseLocalDevYaml(
      fixtureYaml
        .replace("['hot-news:admin']", JSON.stringify(hotNewsRoles))
        .replace("['data-loop:admin']", JSON.stringify(dataLoopRoles)),
    ),
  )
  const settings = loadDevelopment(rootDirectory, {
    VITE_DEV_HOT_NEWS_ROLES: hotNewsRoles.join(','),
    VITE_DEV_DATA_LOOP_ROLES: dataLoopRoles.join(','),
  })
  assert.equal(settings.identityHeaders['X-Hot-News-Roles'], hotNewsRoles.join(','))
  assert.equal(settings.identityHeaders['X-Data-Loop-Roles'], dataLoopRoles.join(','))
})

test('builds, previews, and non-development serves never load local identity or simulation', async (t) => {
  const modes = [
    { command: 'build', mode: 'production' },
    { command: 'build', mode: 'development' },
    { command: 'serve', mode: 'production' },
    { command: 'serve', mode: 'staging' },
    { command: 'serve', mode: 'production', isPreview: true },
    { command: 'serve', mode: 'development', isPreview: true },
  ]
  for (const options of modes) {
    await t.test(JSON.stringify(options), (subtest) => {
      const rootDirectory = fixtureRoot(subtest, 'gateway: [this YAML is broken')
      const settings = loadLocalDevSettings({
        rootDirectory,
        ...options,
        env: {
          VITE_DEV_PORT: '5184',
          VITE_DEV_TENANT_ID: 'invalid-private-tenant',
          VITE_DEV_USER_ID: 'invalid-private-user',
          VITE_DEV_GATEWAY_ENABLED: 'invalid-private-enabled',
          VITE_DEV_GATEWAY_TOKEN: 'newsagent-test-private-canary',
          VITE_DEV_HOT_NEWS_ROLES: 'invalid\r\nprivate-role',
          VITE_LOCAL_SIMULATION: '1',
        },
      })

      assert.equal(settings.configPath, null)
      assert.equal(settings.port, 5184)
      assert.equal(settings.proxyTarget, 'http://127.0.0.1:8000')
      assert.equal(settings.localSimulation, false)
      assert.deepEqual(settings.identityHeaders, {})
      assert.equal(JSON.stringify(settings).includes('newsagent-test-private-canary'), false)
    })
  }
})

test('non-development proxy targets and router bases retain explicit environment overrides', (t) => {
  const rootDirectory = fixtureRoot(t, null)
  const settings = loadLocalDevSettings({
    rootDirectory,
    command: 'build',
    mode: 'production',
    env: {
      VITE_DEV_PROXY_TARGET: 'https://backend.example.test',
      VITE_ROUTER_BASE: '/console/',
    },
  })
  assert.equal(settings.proxyTarget, 'https://backend.example.test')
  assert.equal(settings.port, 5173)
  assert.equal(settings.routerBase, '/console/')
  assert.deepEqual(settings.identityHeaders, {})
})

test('development requires its local configuration file', (t) => {
  const rootDirectory = fixtureRoot(t, null)
  assert.throws(() => loadDevelopment(rootDirectory))
})

test('the parser rejects ambiguous YAML and invalid configuration values', async (t) => {
  const invalidSources = [
    ['invalid YAML', 'gateway: [broken'],
    ['non-object root', '- invalid'],
    ['missing root field', fixtureYaml.replace('simulation:\n  enabled: true\n', '')],
    ['missing gateway token', fixtureYaml.replace(`  token: '${publicToken}'\n`, '')],
    [
      'non-object nested section',
      fixtureYaml.replace('simulation:\n  enabled: true', 'simulation: null'),
    ],
    ['unknown root field', `${fixtureYaml}unexpected: true\n`],
    [
      'unknown nested field',
      fixtureYaml.replace('  port: 5174', '  port: 5174\n  unexpected: true'),
    ],
    ['duplicate field', fixtureYaml.replace('  port: 5174', '  port: 5174\n  port: 5184')],
    ['unsupported schema', fixtureYaml.replace('schema_version: 1', 'schema_version: 2')],
    ['string schema', fixtureYaml.replace('schema_version: 1', "schema_version: '1'")],
    ['string port', fixtureYaml.replace('port: 5174', "port: '5174'")],
    ['zero port', fixtureYaml.replace('port: 5174', 'port: 0')],
    ['port above range', fixtureYaml.replace('port: 5174', 'port: 65536')],
    ['fractional port', fixtureYaml.replace('port: 5174', 'port: 5174.5')],
    ['non-string host', fixtureYaml.replace("host: '127.0.0.1'", 'host: true')],
    ['non-string target', fixtureYaml.replace("target: 'http://127.0.0.1:28000'", 'target: 28000')],
    [
      'simulation string boolean',
      fixtureYaml.replace('simulation:\n  enabled: true', "simulation:\n  enabled: 'true'"),
    ],
    [
      'simulation numeric boolean',
      fixtureYaml.replace('simulation:\n  enabled: true', 'simulation:\n  enabled: 1'),
    ],
    [
      'gateway string boolean',
      fixtureYaml.replace('gateway:\n  enabled: true', "gateway:\n  enabled: 'false'"),
    ],
    ['invalid tenant UUID', fixtureYaml.replace(tenantId, 'invalid-tenant')],
    ['invalid user UUID', fixtureYaml.replace(userId, 'invalid-user')],
    [
      'roles must be arrays',
      fixtureYaml.replace("hot_news_roles: ['hot-news:admin']", "hot_news_roles: 'hot-news:admin'"),
    ],
    [
      'roles must contain strings',
      fixtureYaml.replace("hot_news_roles: ['hot-news:admin']", 'hot_news_roles: [1]'),
    ],
    [
      'role header delimiter',
      fixtureYaml.replace(
        "hot_news_roles: ['hot-news:admin']",
        "hot_news_roles: ['hot-news:admin,forged']",
      ),
    ],
    [
      'unknown hot-news role',
      fixtureYaml.replace(
        "hot_news_roles: ['hot-news:admin']",
        "hot_news_roles: ['hot-news:superuser']",
      ),
    ],
    [
      'role from another service',
      fixtureYaml.replace(
        "hot_news_roles: ['hot-news:admin']",
        "hot_news_roles: ['data-loop:admin']",
      ),
    ],
    [
      'unknown data-loop role',
      fixtureYaml.replace(
        "data_loop_roles: ['data-loop:admin']",
        "data_loop_roles: ['data-loop:superuser']",
      ),
    ],
    [
      'role line break',
      fixtureYaml.replace(
        "hot_news_roles: ['hot-news:admin']",
        'hot_news_roles: ["hot-news:admin\\r\\nInjected: yes"]',
      ),
    ],
    ['token must be a string', fixtureYaml.replace(`token: '${publicToken}'`, 'token: 123')],
    [
      'token line break',
      fixtureYaml.replace(`token: '${publicToken}'`, 'token: "public-canary\\r\\nInjected: yes"'),
    ],
  ]
  for (const [label, source] of invalidSources) {
    await t.test(label, () => {
      assert.throws(() => parseLocalDevYaml(source))
    })
  }
})

test('invalid environment overrides fail instead of falling back to YAML', async (t) => {
  const overrides = [
    { VITE_DEV_PORT: '0' },
    { VITE_DEV_PORT: '65536' },
    { VITE_DEV_PORT: '5174.5' },
    { VITE_DEV_PORT: 'invalid' },
    { VITE_LOCAL_SIMULATION: 'sometimes' },
    { VITE_DEV_GATEWAY_ENABLED: 'sometimes' },
    { VITE_DEV_TENANT_ID: 'invalid-tenant' },
    { VITE_DEV_USER_ID: 'invalid-user' },
    { VITE_DEV_HOT_NEWS_ROLES: 'hot-news:admin\r\nInjected: yes' },
    { VITE_DEV_HOT_NEWS_ROLES: 'hot-news:superuser' },
    { VITE_DEV_DATA_LOOP_ROLES: 'data-loop:admin\r\nInjected: yes' },
    { VITE_DEV_DATA_LOOP_ROLES: 'data-loop:superuser' },
    { VITE_DEV_GATEWAY_TOKEN: 'public-test-canary\r\nInjected: yes' },
  ]
  for (const override of overrides) {
    await t.test(Object.keys(override)[0], (subtest) => {
      const rootDirectory = fixtureRoot(subtest)
      assert.throws(() => loadDevelopment(rootDirectory, override))
    })
  }
})

test('identity proxies accept IPv4, IPv6, and localhost loopback targets', async (t) => {
  const pairs = [
    ['127.0.0.1', 'http://127.0.0.1:28000'],
    ['localhost', 'http://localhost:28000'],
    ['::1', 'http://[::1]:28000'],
  ]
  for (const [host, proxyTarget] of pairs) {
    await t.test(host, () => {
      assert.doesNotThrow(() =>
        assertLocalDevBoundary({
          host,
          proxyTarget,
          identityHeaders: { Authorization: `Bearer ${publicToken}` },
        }),
      )
    })
  }
})

test('identity proxies reject remote or wildcard listeners and unsafe proxy targets', async (t) => {
  const unsafePairs = [
    [true, 'http://127.0.0.1:28000'],
    ['0.0.0.0', 'http://127.0.0.1:28000'],
    ['::', 'http://127.0.0.1:28000'],
    ['example.test', 'http://127.0.0.1:28000'],
    ['127.0.0.1', 'http://example.test:28000'],
    ['127.0.0.1', 'http://127.0.0.1.example.test:28000'],
    ['127.0.0.1', 'ftp://127.0.0.1:28000'],
    ['127.0.0.1', 'http://public-user:public-password@127.0.0.1:28000'],
  ]
  for (const [host, proxyTarget] of unsafePairs) {
    await t.test(`${String(host)} → ${proxyTarget}`, () => {
      assert.throws(() =>
        assertLocalDevBoundary({
          host,
          proxyTarget,
          identityHeaders: { Authorization: `Bearer ${publicToken}` },
        }),
      )
    })
  }
})

test('configuration error messages do not reveal rejected header values', (t) => {
  const rootDirectory = fixtureRoot(t)
  const rejectedToken = 'newsagent-test-secret-canary\r\nInjected: yes'
  assert.throws(
    () => loadDevelopment(rootDirectory, { VITE_DEV_GATEWAY_TOKEN: rejectedToken }),
    (error) => {
      assert.equal(String(error.message).includes('newsagent-test-secret-canary'), false)
      return true
    },
  )
})

test('proxies without injected identity preserve non-loopback compatibility', () => {
  assert.doesNotThrow(() =>
    assertLocalDevBoundary({
      host: '0.0.0.0',
      proxyTarget: 'https://backend.example.test',
      identityHeaders: {},
    }),
  )
})
