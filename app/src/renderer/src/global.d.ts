export {}

type SystemInfo = {
    ip: string
    mac: string
    hostname: string
    os: string
    cpu: string
    cpu_cores: number
    total_memory: number
    disk_serial: string
    username: string
    app_version: string
}

type StreamStatus = { running: boolean; url: string; error: string; reason?: string }
type RegistrationStatus = { ok: boolean; message: string; at: string | null; info?: SystemInfo }

declare global {
    interface Window {
        eyesAPI: {
            getStatus: () => Promise<{
                config: { recordingServiceURL: string; srsHost: string }
                system: SystemInfo
                registration: RegistrationStatus
                stream: StreamStatus
            }>
            registerDevice: () => Promise<RegistrationStatus>
            restartStream: () => Promise<{ ok: boolean; error?: string }>
            onStreamStatus: (callback: (status: StreamStatus) => void) => () => void
            onRegistration: (callback: (status: RegistrationStatus) => void) => () => void
        }
    }
}
