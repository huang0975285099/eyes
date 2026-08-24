/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly QCLI_NATIVE_BUILD?: boolean
  readonly QUASAR_CAPACITOR_MODE?: boolean
  readonly TAURI_ENV_PLATFORM?: string
}
