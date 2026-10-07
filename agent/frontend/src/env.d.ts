/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_APP_TITLE?: string
  readonly VITE_ROUTER_BASE?: string
  readonly VITE_DEV_PROXY_TARGET?: string
  readonly VITE_LOCAL_SIMULATION?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
