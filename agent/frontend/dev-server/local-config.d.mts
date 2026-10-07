export interface LocalDevSettings {
  host: string
  port: number
  proxyTarget: string
  routerBase: string
  localSimulation: boolean
  identityHeaders: Record<string, string>
  configPath: string | null
}

export function loadLocalDevSettings(options: {
  rootDirectory: string
  command: string
  mode: string
  isPreview?: boolean
  env?: Record<string, string>
}): LocalDevSettings

export function assertLocalDevBoundary(options: {
  host: string | boolean | undefined
  proxyTarget: string
  identityHeaders: Record<string, string>
}): void

export function parseLocalDevYaml(source: string): object
