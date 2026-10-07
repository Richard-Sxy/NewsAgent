/** Node-only startup configuration. Never import this module in browser code. */
import { readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { URL } from 'node:url'

import { JSON_SCHEMA, load } from 'js-yaml'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]'])
const HOT_NEWS_ROLES = new Set([
  'hot-news:read',
  'hot-news:decide',
  'hot-news:handoff',
  'hot-news:admin',
])
const DATA_LOOP_ROLES = new Set([
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
])

function fail(field) {
  // Report field names only: a malformed token must never appear in logs.
  throw new Error(`本地前端配置 ${field} 无效，请检查 dev-server/local.yml`)
}

function object(value, fields, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(name)
  if (Object.keys(value).some((field) => !fields.includes(field))) fail(`${name} 包含未知字段`)
  if (fields.some((field) => !Object.hasOwn(value, field))) fail(`${name} 缺少字段`)
  return value
}

function boolean(value, name) {
  if (typeof value !== 'boolean') fail(name)
  return value
}

function text(value, name, { allowEmpty = false } = {}) {
  if (typeof value !== 'string' || value.length > 4096) fail(name)
  if (
    [...value].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)
  )
    fail(name)
  if (!allowEmpty && !value.trim()) fail(name)
  return value.trim()
}

function port(value) {
  if (!Number.isInteger(value) || value < 1 || value > 65535) fail('server.port')
  return value
}

function target(value) {
  const source = text(value, 'proxy.target')
  let url
  try {
    url = new URL(source)
  } catch {
    fail('proxy.target')
  }
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    fail('proxy.target')
  return source
}

function uuid(value, name, allowEmpty = false) {
  const result = text(value, name, { allowEmpty })
  if (result || !allowEmpty) {
    if (!UUID.test(result)) fail(name)
  }
  return result
}

function roles(value, allowed, name) {
  if (!Array.isArray(value) || value.length > 20) fail(name)
  const validated = value.map((item) => text(item, name))
  if (validated.some((item) => !allowed.has(item)) || new Set(validated).size !== validated.length)
    fail(name)
  return validated
}

/** Parse a fixed schema with no tags/merge keys and bounded, redacted errors. */
export function parseLocalDevYaml(source) {
  if (typeof source !== 'string' || source.length > 32768) fail('文件大小')
  let parsed
  try {
    parsed = load(source, { schema: JSON_SCHEMA })
  } catch {
    // js-yaml's original error includes YAML snippets; do not propagate it.
    throw new Error('本地前端 YAML 格式错误，请检查缩进、重复字段和类型')
  }
  const config = object(
    parsed,
    ['schema_version', 'server', 'proxy', 'simulation', 'gateway'],
    'root',
  )
  if (config.schema_version !== 1) fail('schema_version')
  const server = object(config.server, ['host', 'port'], 'server')
  const proxy = object(config.proxy, ['target'], 'proxy')
  const simulation = object(config.simulation, ['enabled'], 'simulation')
  const gateway = object(
    config.gateway,
    ['enabled', 'tenant_id', 'user_id', 'hot_news_roles', 'data_loop_roles', 'token'],
    'gateway',
  )
  return {
    schema_version: 1,
    server: { host: text(server.host, 'server.host'), port: port(server.port) },
    proxy: { target: target(proxy.target) },
    simulation: { enabled: boolean(simulation.enabled, 'simulation.enabled') },
    gateway: {
      enabled: boolean(gateway.enabled, 'gateway.enabled'),
      tenant_id: uuid(gateway.tenant_id, 'gateway.tenant_id', true),
      user_id: uuid(gateway.user_id, 'gateway.user_id', true),
      hot_news_roles: roles(gateway.hot_news_roles, HOT_NEWS_ROLES, 'gateway.hot_news_roles'),
      data_loop_roles: roles(gateway.data_loop_roles, DATA_LOOP_ROLES, 'gateway.data_loop_roles'),
      token: text(gateway.token, 'gateway.token', { allowEmpty: true }),
    },
  }
}

/** Validate the final Vite host as CLI --host can override the YAML host. */
export function assertLocalDevBoundary({ host, proxyTarget, identityHeaders }) {
  if (!Object.keys(identityHeaders).length) return
  const url = new URL(target(proxyTarget))
  if (!LOOPBACK.has(host) || !LOOPBACK.has(url.hostname)) {
    throw new Error(
      '注入本地身份时，前端监听地址和后端代理都必须是环回地址；请勿使用 --host 0.0.0.0 或远程代理',
    )
  }
}

function override(env, name, fallback) {
  return Object.hasOwn(env, name) ? env[name] : fallback
}

function environmentBoolean(env, name, fallback) {
  if (!Object.hasOwn(env, name)) return fallback
  if (!['0', '1', 'true', 'false'].includes(env[name])) fail(name)
  return ['1', 'true'].includes(env[name])
}

function environmentPort(env, fallback) {
  if (!Object.hasOwn(env, 'VITE_DEV_PORT')) return fallback
  if (typeof env.VITE_DEV_PORT !== 'string' || !/^\d+$/.test(env.VITE_DEV_PORT))
    fail('VITE_DEV_PORT')
  return port(Number(env.VITE_DEV_PORT))
}

function environmentRoles(env, name, fallback, allowed) {
  if (!Object.hasOwn(env, name)) return fallback
  const source = text(env[name], name, { allowEmpty: true })
  return roles(source ? source.split(',').map((item) => item.trim()) : [], allowed, name)
}

/** YAML defaults < existing VITE_DEV_* overrides; no mutation of process.env. */
export function loadLocalDevSettings({
  rootDirectory,
  command,
  mode,
  isPreview = false,
  env = {},
}) {
  const local = command === 'serve' && mode === 'development' && !isPreview
  let config = null
  const configPath = local ? join(rootDirectory, 'dev-server', 'local.yml') : null
  if (configPath) {
    let source
    try {
      if (statSync(configPath).size > 32768) fail('文件大小')
      source = readFileSync(configPath, 'utf8')
    } catch {
      throw new Error('无法读取本地前端配置 dev-server/local.yml；请确认文件存在且小于32KB')
    }
    config = parseLocalDevYaml(source)
  }
  const settings = {
    host: config?.server.host ?? '127.0.0.1',
    port: environmentPort(env, config?.server.port ?? 5173),
    proxyTarget: target(
      override(env, 'VITE_DEV_PROXY_TARGET', config?.proxy.target ?? 'http://127.0.0.1:8000'),
    ),
    routerBase: text(override(env, 'VITE_ROUTER_BASE', '/'), 'VITE_ROUTER_BASE'),
    localSimulation: config
      ? environmentBoolean(env, 'VITE_LOCAL_SIMULATION', config.simulation.enabled)
      : false,
    identityHeaders: {},
    configPath,
  }
  if (!settings.routerBase.startsWith('/') || settings.routerBase.startsWith('//'))
    fail('VITE_ROUTER_BASE')
  if (!config || !environmentBoolean(env, 'VITE_DEV_GATEWAY_ENABLED', config.gateway.enabled))
    return settings
  const tenant = uuid(
    override(env, 'VITE_DEV_TENANT_ID', config.gateway.tenant_id),
    'VITE_DEV_TENANT_ID',
    true,
  )
  const user = uuid(
    override(env, 'VITE_DEV_USER_ID', config.gateway.user_id),
    'VITE_DEV_USER_ID',
    true,
  )
  const hotRoles = environmentRoles(
    env,
    'VITE_DEV_HOT_NEWS_ROLES',
    config.gateway.hot_news_roles,
    HOT_NEWS_ROLES,
  )
  const dataRoles = environmentRoles(
    env,
    'VITE_DEV_DATA_LOOP_ROLES',
    config.gateway.data_loop_roles,
    DATA_LOOP_ROLES,
  )
  const token = text(
    override(env, 'VITE_DEV_GATEWAY_TOKEN', config.gateway.token),
    'VITE_DEV_GATEWAY_TOKEN',
    { allowEmpty: true },
  )
  if (tenant) settings.identityHeaders['X-Tenant-ID'] = tenant
  if (user) settings.identityHeaders['X-User-ID'] = user
  if (hotRoles.length) settings.identityHeaders['X-Hot-News-Roles'] = hotRoles.join(',')
  if (dataRoles.length) settings.identityHeaders['X-Data-Loop-Roles'] = dataRoles.join(',')
  if (token) settings.identityHeaders.Authorization = `Bearer ${token}`
  assertLocalDevBoundary(settings)
  return settings
}
