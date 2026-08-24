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
    user_name: string
    app_version: string
}

type VideoSourceStatus = {
    id: string
    type: 'screen' | 'usb_camera' | 'ip_camera'
    displayName: string
    running: boolean
    url: string
    error: string
    reason?: string
}
type StreamStatus = {
    running: boolean
    url: string
    error: string
    sources: VideoSourceStatus[]
}
type NVRSource = {
    id: string
    type: 'ip_camera'
    displayName: string
    url: string
    transport: 'tcp' | 'udp'
    enabled: boolean
}
declare global {
    interface Window {
        eyesAPI: {
            getStatus: () => Promise<{
                config: { mediaServiceURL: string }
                system: SystemInfo
                stream: StreamStatus
            }>
            setUserName: (value: string) => Promise<string>
            restartStream: () => Promise<{ ok: boolean; error?: string }>
            getNVRConfig: () => Promise<{ sources: NVRSource[]; stream: StreamStatus }>
            saveNVRConfig: (sources: NVRSource[]) => Promise<{
                sources: NVRSource[]
                stream: StreamStatus
                result: { ok: boolean; error?: string }
            }>
            onStreamStatus: (callback: (status: StreamStatus) => void) => () => void
            onNavigate: (callback: (page: 'status' | 'nvr') => void) => () => void
        }
    }
}
