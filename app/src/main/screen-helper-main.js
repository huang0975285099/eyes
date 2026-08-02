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

function publicURL(url) {
    return String(url || '').replace(/([?&]token=)[^&]+/i, '$1***')
}

export function setupScreenHelper({ recordingServiceURL, clientApiKey, onStatus }) {
    let proc = null
    let stopping = false
    let restartTimer = null
    let state = { running: false, url: '', error: '' }

    function update(next) {
        state = { ...state, ...next }
        onStatus?.({ ...state })
    }

    async function getPublishConfig(mac) {
        if (!recordingServiceURL || !clientApiKey) throw new Error('RecordingService 配置不完整')
        const response = await fetch(
            `${recordingServiceURL.replace(/\/$/, '')}/api/streams/publish-config`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Client-Key': clientApiKey },
                body: JSON.stringify({ mac }),
                signal: AbortSignal.timeout(5000)
            }
        )
        if (!response.ok) throw new Error(`申请推流令牌失败 HTTP ${response.status}`)
        const body = await response.json()
        if (!body.rtmp_url) throw new Error('RecordingService 未返回 rtmp_url')
        return body
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
            const nic = await getPreferredNICAsync(recordingServiceURL)
            if (!nic.mac) throw new Error('未找到可用网卡 MAC 地址')
            const publishConfig = await getPublishConfig(nic.mac)
            const rtmp = publishConfig.rtmp_url
            const args = [
                '--electron-spawned',
                '--mode',
                'monitor',
                '--fps',
                '8',
                '--server-url',
                recordingServiceURL,
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
