export interface AccountUser {
  id: number
  username: string
  role: string
  customer_id: number
  customer_name: string
}

export interface VideoSource {
  video_source_id: number
  customer_id: number
  stream_name: string
  display_name: string
  source_type: string
  source_id: string
  mac: string
  operator_name: string
  hostname: string
  local_ip: string
  brand: string
  publish_mode: string
  enabled: boolean
  active: boolean
  codec: string
  width: number
  height: number
  playback_url?: string
  recording_enabled: boolean
  recording_retain_hours: number
  sampling_enabled: boolean
  sampling_interval_minutes: number
  sampling_frame_count: number
  sampling_retain_hours: number
  frame_count: number
  last_captured_at?: string
}

export interface FrameResult {
  id: number
  stream_name: string
  display_name: string
  captured_at: string
  file_size: number
}

export interface RecordingSegment {
  id: number
  stream_name: string
  display_name: string
  started_at: string
  ended_at: string
  duration: number
  file_size: number
}

const SERVER_KEY = 'eyes_customer_server'
const TOKEN_KEY = 'eyes_customer_session'

function normalizedServer(value: string): string {
  return value.trim().replace(/\/+$/, '')
}

export function defaultServer(): string {
  const stored = localStorage.getItem(SERVER_KEY)
  if (stored) {
    if (['http://10.0.20.219:11111', 'http://112.18.238.6:11111'].includes(stored)) {
      const migrated = 'http://112.18.238.6:18887'
      localStorage.setItem(SERVER_KEY, migrated)
      return migrated
    }
    return stored
  }
  // Browser deployment can use its own origin, but the packaged Tauri app
  // is served from tauri.localhost and must connect to the remote API.
  if (location.pathname.startsWith('/customer') && location.hostname !== 'tauri.localhost') return location.origin
  return 'http://112.18.238.6:18887'
}

export function saveServer(value: string): void {
  localStorage.setItem(SERVER_KEY, normalizedServer(value))
}

export function clearSession(): void { localStorage.removeItem(TOKEN_KEY) }

export function customerResourceURL(server: string, path: string): string {
  return `${normalizedServer(server)}/api/customer${path}`
}

function isCrossOrigin(server: string): boolean {
  try { return new URL(server).origin !== location.origin } catch { return true }
}

export async function api<T>(server: string, path: string, options: RequestInit = {}): Promise<T> {
  const base = normalizedServer(server)
  const token = localStorage.getItem(TOKEN_KEY) || ''
  const crossOrigin = isCrossOrigin(base)
  const response = await fetch(`${base}/api/customer${path}`, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(crossOrigin ? { 'X-Eyes-Native-App': '1' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  })
  const payload = await response.json().catch(() => ({})) as Record<string, unknown>
  if (!response.ok) {
    const raw = String(payload.error || `请求失败 HTTP ${response.status}`)
    const nested = raw.indexOf('{')
    let message = raw
    if (nested >= 0) {
      try { message = String((JSON.parse(raw.slice(nested)) as { error?: string }).error || raw) } catch { /* keep raw */ }
    }
    const error = new Error(message) as Error & { status?: number }
    error.status = response.status
    throw error
  }
  if (typeof payload.session_token === 'string' && payload.session_token) {
    localStorage.setItem(TOKEN_KEY, payload.session_token)
    delete payload.session_token
  }
  return payload as T
}
