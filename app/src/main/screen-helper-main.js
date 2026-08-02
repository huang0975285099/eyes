import { app, powerMonitor } from 'electron'
import { spawn } from 'child_process'
import { existsSync } from 'fs'
import path from 'path'
import { getPreferredNICAsync } from './network-util'

function helperPath() {
    return app.isPackaged
        ? path.join(process.resourcesPath, 'screen-helper.exe')
        : path.join(app.getAppPath(), 'resources', 'screen-helper.exe')
}

function parseHost(value) {
    const raw = String(value || '').trim()
    if (!raw) return { host: '', port: '1935' }
    try {
        const url = new URL(raw.includes('://') ? raw : `rtmp://${raw}`)
        return { host: url.hostname, port: url.port || '1935' }
    } catch {
        return { host: '', port: '1935' }
    }
}

function publicURL(url) {
    return String(url || '').replace(/([?&]token=)[^&]+/i, '$1***')
}

export function setupScreenHelper({ hostUrl, srsHost, clientApiKey, onStatus }) {
    let proc = null
    let stopping = false
    let restartTimer = null
    let state = { running: false, url: '', error: '' }

    function update(next) {
        state = { ...state, ...next }
        onStatus?.({ ...state })
    }

    async function token(mac) {
        if (!hostUrl || !clientApiKey) return ''
        const response = await fetch(`${hostUrl.replace(/\/$/, '')}/api/srs/stream-token`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Client-Key': clientApiKey },
            body: JSON.stringify({ mac }),
            signal: AbortSignal.timeout(5000)
        })
        if (!response.ok) throw new Error(`申请推流令牌失败 HTTP ${response.status}`)
        const body = await response.json()
        return body.token || body.data?.token || ''
    }

    async function start(reason = 'manual') {
        stopping = false
        if (proc && !proc.killed) return { ok: true }
        const executable = helperPath()
        if (!existsSync(executable)) {
            update({ running: false, error: `采集程序不存在: ${executable}` })
            return { ok: false, error: state.error }
        }
        try {
            const nic = await getPreferredNICAsync(hostUrl)
            if (!nic.mac) throw new Error('未找到可用网卡 MAC 地址')
            const endpoint = parseHost(srsHost)
            if (!endpoint.host) throw new Error('未配置 srsHost')
            const streamName = nic.mac.replace(/[:-]/g, '').toLowerCase()
            const streamToken = await token(nic.mac)
            const query = streamToken ? `?token=${encodeURIComponent(streamToken)}` : ''
            const rtmp = `rtmp://${endpoint.host}:${endpoint.port}/live/${streamName}${query}`
            const args = [
                '--electron-spawned',
                '--mode',
                'monitor',
                '--fps',
                '8',
                '--server-url',
                hostUrl,
                '--rtmp',
                rtmp
            ]
            proc = spawn(executable, args, {
                cwd: path.dirname(executable),
                windowsHide: true,
                stdio: ['ignore', 'pipe', 'pipe']
            })
            proc.stdout?.on('data', (data) => console.log(`[screen] ${data.toString().trim()}`))
            proc.stderr?.on('data', (data) => console.error(`[screen] ${data.toString().trim()}`))
            proc.on('error', (error) => update({ running: false, error: error.message }))
            proc.on('exit', (code) => {
                proc = null
                update({
                    running: false,
                    error: stopping ? '' : `采集进程退出 (${code ?? 'unknown'})`
                })
                if (!stopping) restartTimer = setTimeout(() => start('auto_restart'), 5000)
            })
            update({ running: true, url: publicURL(rtmp), error: '', reason })
            return { ok: true }
        } catch (error) {
            update({ running: false, error: error.message })
            return { ok: false, error: error.message }
        }
    }

    function stop() {
        stopping = true
        if (restartTimer) clearTimeout(restartTimer)
        restartTimer = null
        proc?.kill()
        proc = null
        update({ running: false, url: '', error: '' })
    }

    async function restart(reason = 'manual') {
        stop()
        await new Promise((resolve) => setTimeout(resolve, 1000))
        return start(reason)
    }

    powerMonitor.on('lock-screen', () => stop('lock_screen'))
    powerMonitor.on('unlock-screen', () => setTimeout(() => start('unlock_screen'), 2000))
    powerMonitor.on('resume', () => setTimeout(() => restart('resume'), 2000))

    return { start, stop, restart, status: () => ({ ...state }) }
}
