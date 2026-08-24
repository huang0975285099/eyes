import { app, powerMonitor } from 'electron'
import { spawn } from 'child_process'
import { existsSync } from 'fs'
import path from 'path'
import { getPreferredNICAsync } from './network-util'

const SOURCE_LABELS = {
    screen: '电脑桌面',
    usb_camera: 'USB 摄像头',
    ip_camera: '网络摄像头'
}

function helperPath() {
    return app.isPackaged
        ? path.join(process.resourcesPath, 'screen-helper.exe')
        : path.join(app.getAppPath(), 'resources', 'screen-helper.exe')
}

function ffmpegPath() {
    return app.isPackaged
        ? path.join(process.resourcesPath, 'ffmpeg.exe')
        : path.join(app.getAppPath(), 'resources', 'ffmpeg.exe')
}

function publicURL(url) {
    return String(url || '')
}

function redact(value, source) {
    let text = String(value || '')
    if (source.url) text = text.replaceAll(source.url, '[camera-url]')
    return text.replace(/([a-z]+:\/\/)([^\s/@]+)@/gi, '$1***@')
}

function clampNumber(value, fallback, min, max) {
    const parsed = Number.parseInt(value, 10)
    if (!Number.isFinite(parsed)) return fallback
    return Math.min(max, Math.max(min, parsed))
}

function normalizeVideoSources(configuredSources) {
    const raw = Array.isArray(configuredSources)
        ? configuredSources
        : [{ id: 'desktop', type: 'screen', displayName: SOURCE_LABELS.screen }]
    const ids = new Set()
    return raw
        .filter((source) => source && source.enabled !== false)
        .map((source, index) => {
            const type = String(source.type || 'screen')
                .trim()
                .toLowerCase()
            if (!SOURCE_LABELS[type]) throw new Error(`不支持的视频源类型: ${type}`)
            const id = String(source.id || (type === 'screen' ? 'desktop' : '')).trim()
            if (!id) throw new Error(`第 ${index + 1} 个摄像头缺少 id`)
            if (ids.has(id)) throw new Error(`视频源 id 重复: ${id}`)
            ids.add(id)
            if (type === 'usb_camera' && !String(source.deviceName || '').trim()) {
                throw new Error(`USB 摄像头 ${id} 缺少 deviceName`)
            }
            if (type === 'ip_camera') {
                const cameraURL = String(source.url || '').trim()
                if (!/^(rtsp|rtsps|http|https):\/\//i.test(cameraURL)) {
                    throw new Error(`网络摄像头 ${id} 的 url 必须使用 RTSP 或 HTTP(S)`)
                }
            }
            return {
                ...source,
                id,
                type,
                displayName: String(source.displayName || SOURCE_LABELS[type]).trim(),
                fps: clampNumber(source.fps, type === 'screen' ? 8 : 15, 1, 30),
                bitrateKbps: clampNumber(source.bitrateKbps, 1200, 200, 8000),
                maxWidth: clampNumber(source.maxWidth, 1280, 320, 3840)
            }
        })
}

function cameraFFmpegArgs(source, rtmpURL) {
    const args = ['-hide_banner', '-nostdin']
    if (source.type === 'usb_camera') {
        args.push('-f', 'dshow', '-rtbufsize', '256M')
        if (source.fps) args.push('-framerate', String(source.fps))
        if (source.resolution && /^\d+x\d+$/.test(source.resolution)) {
            args.push('-video_size', source.resolution)
        }
        args.push('-i', `video=${source.deviceName}`)
    } else {
        args.push('-rw_timeout', '15000000')
        if (/^rtsps?:\/\//i.test(source.url)) {
            const transport = source.transport === 'udp' ? 'udp' : 'tcp'
            args.push('-rtsp_transport', transport)
        } else {
            args.push('-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5')
        }
        args.push('-i', source.url)
    }

    // RTSP/NVR网络摄像头保留原始H.264/H.265码流，只从RTSP重封装到
    // Enhanced RTMP。不解码、不缩放、不重新编码，大幅降低现场电脑CPU占用。
    if (source.type === 'ip_camera') {
        args.push(
            '-map',
            '0:v:0',
            '-an',
            '-c:v',
            'copy',
            '-flvflags',
            'no_duration_filesize',
            '-rtmp_enhanced_codecs',
            'hvc1',
            '-loglevel',
            'warning',
            '-f',
            'flv',
            rtmpURL
        )
        return args
    }

    const bitrate = `${source.bitrateKbps}k`
    const bufferSize = `${source.bitrateKbps * 2}k`
    args.push(
        '-an',
        '-vf',
        `scale=w='min(iw,${source.maxWidth})':h=-2,format=yuv420p`,
        '-c:v',
        'libx264',
        '-preset',
        'ultrafast',
        '-tune',
        'zerolatency',
        '-profile:v',
        'baseline',
        '-b:v',
        bitrate,
        '-maxrate',
        bitrate,
        '-bufsize',
        bufferSize,
        '-r',
        String(source.fps),
        '-g',
        String(source.fps),
        '-keyint_min',
        String(source.fps),
        '-sc_threshold',
        '0',
        '-loglevel',
        'warning',
        '-f',
        'flv',
        rtmpURL
    )
    return args
}

export function setupScreenHelper({
    mediaServiceURL,
    videoSources,
    getDeviceMetadata,
    onOperatorName,
    onStatus
}) {
    let sources
    let configurationError = ''
    try {
        sources = normalizeVideoSources(videoSources)
    } catch (error) {
        sources = []
        configurationError = error.message
    }
    const processes = new Map()
    const restartTimers = new Map()
    const suspendedSources = new Set()
    const sourceStates = new Map(
        sources.map((source) => [
            source.id,
            {
                id: source.id,
                type: source.type,
                displayName: source.displayName,
                running: false,
                url: '',
                error: ''
            }
        ])
    )
    let stopping = false

    function status() {
        const sourceList = [...sourceStates.values()].map((state) => ({ ...state }))
        const primary = sourceList.find((source) => source.running) || sourceList[0]
        const errors = sourceList.filter((source) => source.error).map((source) => source.error)
        if (configurationError) errors.unshift(configurationError)
        return {
            running: sourceList.some((source) => source.running),
            url: primary?.url || '',
            error: errors.join('；'),
            sources: sourceList
        }
    }

    function updateSource(id, next) {
        const current = sourceStates.get(id)
        if (!current) return
        sourceStates.set(id, { ...current, ...next })
        onStatus?.(status())
    }

    async function getPublishConfig(mac, source, metadataOverride = {}) {
        if (!mediaServiceURL) throw new Error('MediaService 配置不完整')
        const metadata = { ...(await getDeviceMetadata?.()), ...metadataOverride }
        const response = await fetch(
            `${mediaServiceURL.replace(/\/$/, '')}/api/streams/publish-config`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mac,
                    source_type: source.type,
                    source_id: source.id,
                    display_name: source.displayName,
                    operator_name: metadata.operator_name || '',
                    operator_name_force: metadata.operator_name_force === true,
                    hostname: metadata.hostname || '',
                    local_ip: metadata.local_ip || ''
                }),
                signal: AbortSignal.timeout(5000)
            }
        )
        const body = await response.json().catch(() => ({}))
        if (!response.ok)
            throw new Error(body.message || `申请推流地址失败 HTTP ${response.status}`)
        if (!body.rtmp_url) throw new Error('MediaService 未返回 rtmp_url')
        if (source.type === 'screen' && typeof body.operator_name === 'string') {
            onOperatorName?.(body.operator_name)
        }
        return body
    }

    async function syncOperatorName(value) {
        const operatorName = String(value || '').trim()
        if (!operatorName) throw new Error('点位负责人不能为空')
        const nic = await getPreferredNICAsync(mediaServiceURL)
        if (!nic.mac) throw new Error('未找到可用网卡 MAC 地址')
        const results = await Promise.all(
            sources.map((source) =>
                getPublishConfig(nic.mac, source, {
                    operator_name: operatorName,
                    operator_name_force: true,
                    local_ip: nic.ip || ''
                })
            )
        )
        onOperatorName?.(operatorName)
        return { ok: true, updated: results.length }
    }

    async function refreshOperatorName() {
        const source = sources.find((item) => item.type === 'screen')
        if (!source) return { ok: true, skipped: true }
        const nic = await getPreferredNICAsync(mediaServiceURL)
        if (!nic.mac) throw new Error('未找到可用网卡 MAC 地址')
        await getPublishConfig(nic.mac, source, { local_ip: nic.ip || '' })
        return { ok: true }
    }

    function scheduleRestart(source) {
        if (stopping || suspendedSources.has(source.id) || restartTimers.has(source.id)) return
        const timer = setTimeout(() => {
            restartTimers.delete(source.id)
            startSource(source, 'auto_restart')
        }, 5000)
        restartTimers.set(source.id, timer)
    }

    async function startSource(source, reason, mac) {
        if (suspendedSources.has(source.id)) return { ok: true }
        const existing = processes.get(source.id)
        if (existing && !existing.killed) return { ok: true }
        try {
            let sourceMAC = mac
            if (!sourceMAC) {
                const nic = await getPreferredNICAsync(mediaServiceURL)
                sourceMAC = nic.mac
            }
            if (!sourceMAC) throw new Error('未找到可用网卡 MAC 地址')
            const publishConfig = await getPublishConfig(sourceMAC, source)
            if (suspendedSources.has(source.id)) return { ok: true }
            const rtmpURL = publishConfig.rtmp_url
            let executable
            let args
            if (source.type === 'screen') {
                executable = helperPath()
                args = [
                    '--electron-spawned',
                    '--fps',
                    String(source.fps),
                    '--server-url',
                    mediaServiceURL,
                    '--rtmp',
                    rtmpURL
                ]
            } else {
                executable = ffmpegPath()
                args = cameraFFmpegArgs(source, rtmpURL)
            }
            if (!existsSync(executable)) throw new Error(`采集程序不存在: ${executable}`)
            const proc = spawn(executable, args, {
                cwd: path.dirname(executable),
                windowsHide: true,
                stdio: ['ignore', 'pipe', 'pipe']
            })
            processes.set(source.id, proc)
            proc.stdout?.on('data', (data) =>
                console.log(`[stream:${source.id}] ${redact(data.toString().trim(), source)}`)
            )
            proc.stderr?.on('data', (data) =>
                console.error(`[stream:${source.id}] ${redact(data.toString().trim(), source)}`)
            )
            proc.on('error', (error) => {
                if (processes.get(source.id) !== proc) return
                updateSource(source.id, { running: false, error: error.message })
            })
            proc.on('exit', (code) => {
                if (processes.get(source.id) !== proc) return
                processes.delete(source.id)
                updateSource(source.id, {
                    running: false,
                    error: stopping
                        ? ''
                        : `${source.displayName}采集进程退出 (${code ?? 'unknown'})`
                })
                scheduleRestart(source)
            })
            updateSource(source.id, {
                running: true,
                url: publicURL(rtmpURL),
                error: '',
                reason
            })
            return { ok: true }
        } catch (error) {
            updateSource(source.id, {
                running: false,
                error: `${source.displayName}: ${error.message}`
            })
            scheduleRestart(source)
            return { ok: false, error: error.message }
        }
    }

    async function start(reason = 'manual') {
        stopping = false
        if (configurationError) {
            onStatus?.(status())
            return { ok: false, error: configurationError }
        }
        const nic = await getPreferredNICAsync(mediaServiceURL)
        if (!nic.mac) {
            const error = '未找到可用网卡 MAC 地址'
            for (const source of sources) updateSource(source.id, { running: false, error })
            return { ok: false, error }
        }
        const results = await Promise.all(
            sources.map((source) => startSource(source, reason, nic.mac))
        )
        return {
            ok: results.every((result) => result.ok),
            error: results
                .filter((result) => !result.ok)
                .map((result) => result.error)
                .join('；')
        }
    }

    function stop() {
        stopping = true
        suspendedSources.clear()
        for (const timer of restartTimers.values()) clearTimeout(timer)
        restartTimers.clear()
        for (const proc of processes.values()) proc.kill()
        processes.clear()
        for (const source of sources) {
            updateSource(source.id, { running: false, url: '', error: '' })
        }
    }

    function suspendScreenSources() {
        for (const source of sources.filter((item) => item.type === 'screen')) {
            suspendedSources.add(source.id)
            const timer = restartTimers.get(source.id)
            if (timer) clearTimeout(timer)
            restartTimers.delete(source.id)
            const proc = processes.get(source.id)
            if (proc) {
                processes.delete(source.id)
                proc.kill()
            }
            updateSource(source.id, { running: false, url: '', error: '' })
        }
    }

    function resumeScreenSources() {
        for (const source of sources.filter((item) => item.type === 'screen')) {
            suspendedSources.delete(source.id)
            startSource(source, 'unlock_screen')
        }
    }

    async function restart(reason = 'manual') {
        stop()
        await new Promise((resolve) => setTimeout(resolve, 1000))
        return start(reason)
    }

    const handleUnlockScreen = () => setTimeout(resumeScreenSources, 2000)
    const handleResume = () => setTimeout(() => restart('resume'), 2000)
    powerMonitor.on('lock-screen', suspendScreenSources)
    powerMonitor.on('unlock-screen', handleUnlockScreen)
    powerMonitor.on('resume', handleResume)

    function dispose() {
        stop()
        powerMonitor.removeListener('lock-screen', suspendScreenSources)
        powerMonitor.removeListener('unlock-screen', handleUnlockScreen)
        powerMonitor.removeListener('resume', handleResume)
    }

    return {
        start,
        stop,
        restart,
        syncOperatorName,
        refreshOperatorName,
        status,
        dispose
    }
}
