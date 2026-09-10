/**
 * 运行时配置：容器启动时由 ConfigMap 覆盖 public/config.json，
 * 因此同一份镜像可以部署到测试与生产，不需要重新构建。
 */
export interface RuntimeConfig {
  /** 同源部署时留空字符串；跨域时填写网关绝对地址。 */
  apiBaseUrl: string
  appTitle: string
  /** 由网关注入身份时的兜底租户；正常情况下保持为空。 */
  tenantId: string
  enableHotNews: boolean
  environment: string
  /** 空字符串表示关闭前端错误上报。 */
  errorReportingDsn: string
}

const DEFAULTS: RuntimeConfig = {
  apiBaseUrl: '',
  appTitle: 'NewsAgent 运营控制台',
  tenantId: '',
  enableHotNews: false,
  environment: 'development',
  errorReportingDsn: '',
}

let cached: RuntimeConfig | null = null

function normalize(raw: Partial<RuntimeConfig>): RuntimeConfig {
  return {
    apiBaseUrl: (raw.apiBaseUrl ?? DEFAULTS.apiBaseUrl).replace(/\/+$/, ''),
    appTitle: raw.appTitle ?? DEFAULTS.appTitle,
    tenantId: raw.tenantId ?? DEFAULTS.tenantId,
    enableHotNews: raw.enableHotNews ?? DEFAULTS.enableHotNews,
    environment: raw.environment ?? DEFAULTS.environment,
    errorReportingDsn: raw.errorReportingDsn ?? DEFAULTS.errorReportingDsn,
  }
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (cached) return cached
  try {
    const response = await fetch(`${import.meta.env.BASE_URL}config.json`, { cache: 'no-store' })
    if (!response.ok) throw new Error(`config.json -> HTTP ${response.status}`)
    cached = normalize((await response.json()) as Partial<RuntimeConfig>)
  } catch (error) {
    console.warn('[runtime] 无法读取运行时配置，使用内置默认值', error)
    cached = { ...DEFAULTS }
  }
  return cached
}

export function runtimeConfig(): RuntimeConfig {
  if (!cached) {
    throw new Error('运行时配置尚未加载，请在挂载应用前 await loadRuntimeConfig()')
  }
  return cached
}
