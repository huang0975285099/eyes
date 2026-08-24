import { app, BrowserWindow, ipcMain, Tray, Menu, dialog } from 'electron'
import { execFile, spawn } from 'child_process'
import { createHash, randomUUID } from 'crypto'
import { existsSync, readFileSync, writeFileSync } from 'fs'
import os from 'os'
import { join } from 'path'
import icon from '../../resources/icon.png?asset'
import { setupScreenHelper } from './screen-helper-main'
import { getPreferredNICAsync } from './network-util'

const DEFAULT_CONFIG = {
    mediaServiceURL: 'http://10.0.20.219:22222',
    userName: '',
    videoSources: [{ id: 'desktop', type: 'screen', displayName: '电脑桌面', enabled: true }]
}

let mainWindow
let tray
let screenController
let startHidden = false
let streamConfigurationPromise = Promise.resolve()

function configureAutoLaunch() {
    // 仅对安装后的正式版本启用，开发环境不应修改用户的登录启动项。
    if (process.platform !== 'win32' || !app.isPackaged) return
    app.setLoginItemSettings({
        openAtLogin: true,
        openAsHidden: true,
        args: ['--hidden']
    })
}

function showMainWindow() {
    if (!mainWindow || mainWindow.isDestroyed()) return
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.show()
    mainWindow.focus()
}

function showMainPage(page = 'status') {
    showMainWindow()
    if (!mainWindow || mainWindow.isDestroyed()) return
    const navigate = () => mainWindow?.webContents.send('app:navigate', page)
    if (mainWindow.webContents.isLoading()) mainWindow.webContents.once('did-finish-load', navigate)
    else navigate()
}

function loadConfig() {
    const candidates =
        process.env.NODE_ENV === 'development'
            ? [join(app.getAppPath(), 'config.json')]
            : [
                  join(app.getPath('exe'), '..', 'config.json'),
                  join(process.resourcesPath, 'config.json')
              ]
    let loaded = { ...DEFAULT_CONFIG }
    for (const file of [...candidates, join(app.getPath('userData'), 'config.json')]) {
        try {
            if (existsSync(file)) {
                const fileConfig = JSON.parse(readFileSync(file, 'utf8'))
                loaded = { ...loaded, ...fileConfig }
            }
        } catch (error) {
            console.error(`[config] 读取失败 ${file}:`, error.message)
        }
    }
    return loaded
}

const config = loadConfig()

function saveUserConfig(patch) {
    const file = join(app.getPath('userData'), 'config.json')
    let userConfig = {}
    try {
        if (existsSync(file)) userConfig = JSON.parse(readFileSync(file, 'utf8'))
    } catch (error) {
        console.error(`[config] 保留用户配置失败 ${file}:`, error.message)
    }
    Object.assign(config, patch)
    writeFileSync(file, JSON.stringify({ ...userConfig, ...patch }, null, 2), 'utf8')
}

function getDiskSerial() {
    if (process.platform !== 'win32') return Promise.resolve('')
    return new Promise((resolve) => {
        execFile(
            'powershell.exe',
            [
                '-NoProfile',
                '-NonInteractive',
                '-Command',
                '(Get-Partition -DriveLetter C | Get-Disk).SerialNumber'
            ],
            { windowsHide: true, timeout: 5000 },
            (_error, stdout) => resolve((stdout || '').trim().split(/\r?\n/)[0] || '')
        )
    })
}

async function collectSystemInfo() {
    const nic = await getPreferredNICAsync(config.mediaServiceURL)
    const cpus = os.cpus()
    return {
        ip: nic.ip,
        mac: nic.mac,
        hostname: os.hostname(),
        os: `${os.type()} ${os.release()} ${os.arch()}`,
        cpu: cpus[0]?.model?.trim() || '',
        cpu_cores: cpus.length,
        total_memory: os.totalmem(),
        disk_serial: await getDiskSerial(),
        username: os.userInfo().username,
        user_name: config.userName || '',
        app_version: app.getVersion()
    }
}

function saveUserName(value) {
    const userName = String(value || '').trim()
    if ([...userName].length > 20) throw new Error('姓名或编号不能超过20个字符')
    saveUserConfig({ userName })
    return userName
}

function nvrSources() {
    return (Array.isArray(config.videoSources) ? config.videoSources : []).filter(
        (source) => source?.type === 'ip_camera'
    )
}

function normalizeNVRSource(value, index, occupiedIDs) {
    if (!value || typeof value !== 'object') throw new Error(`第 ${index + 1} 个通道配置无效`)
    let id = String(value.id || '').trim()
    if (!id) id = `nvr-${randomUUID().slice(0, 8)}`
    if ([...id].length > 100) throw new Error(`第 ${index + 1} 个通道ID不能超过100个字符`)
    if (occupiedIDs.has(id)) throw new Error(`通道ID重复：${id}`)
    occupiedIDs.add(id)

    const displayName = String(value.displayName || '').trim()
    if (!displayName) throw new Error(`第 ${index + 1} 个通道缺少名称`)
    if ([...displayName].length > 100)
        throw new Error(`第 ${index + 1} 个通道名称不能超过100个字符`)

    const cameraURL = String(value.url || '').trim()
    let parsedURL
    try {
        parsedURL = new URL(cameraURL)
    } catch {
        throw new Error(`第 ${index + 1} 个通道RTSP地址格式错误`)
    }
    if (!['rtsp:', 'rtsps:'].includes(parsedURL.protocol.toLowerCase())) {
        throw new Error(`第 ${index + 1} 个通道必须使用rtsp://或rtsps://地址`)
    }

    return {
        id,
        type: 'ip_camera',
        displayName,
        url: cameraURL,
        transport: value.transport === 'udp' ? 'udp' : 'tcp',
        enabled: value.enabled !== false
    }
}

function createScreenController() {
    return setupScreenHelper({
        mediaServiceURL: config.mediaServiceURL,
        videoSources: config.videoSources,
        onStatus: (status) => mainWindow?.webContents.send('stream:status-changed', status)
    })
}

async function saveNVRConfiguration(values) {
    if (!Array.isArray(values)) throw new Error('NVR通道配置必须是数组')
    if (values.length > 64) throw new Error('单台客户端最多配置64个NVR通道')
    const retainedSources = (Array.isArray(config.videoSources) ? config.videoSources : []).filter(
        (source) => source?.type !== 'ip_camera'
    )
    const occupiedIDs = new Set(retainedSources.map((source) => String(source.id || '').trim()))
    const normalized = values.map((source, index) => normalizeNVRSource(source, index, occupiedIDs))

    saveUserConfig({ videoSources: [...retainedSources, ...normalized] })
    screenController?.dispose()
    screenController = createScreenController()
    const result = await screenController.start('nvr_config_saved')
    return { sources: nvrSources(), stream: screenController.status(), result }
}

function compareVersions(left, right) {
    const a = String(left)
        .split('.')
        .map((part) => Number.parseInt(part, 10) || 0)
    const b = String(right)
        .split('.')
        .map((part) => Number.parseInt(part, 10) || 0)
    const length = Math.max(a.length, b.length)
    for (let index = 0; index < length; index += 1) {
        if ((a[index] || 0) > (b[index] || 0)) return 1
        if ((a[index] || 0) < (b[index] || 0)) return -1
    }
    return 0
}

async function checkClientUpdate() {
    const baseURL = config.mediaServiceURL.replace(/\/$/, '')
    const response = await fetch(`${baseURL}/api/client-updates/latest`, {
        signal: AbortSignal.timeout(8000)
    })
    if (response.status === 404) return { available: false, currentVersion: app.getVersion() }
    if (!response.ok) throw new Error(`检查更新失败 HTTP ${response.status}`)
    const update = await response.json()
    return {
        ...update,
        available: compareVersions(update.version, app.getVersion()) > 0,
        currentVersion: app.getVersion()
    }
}

async function downloadAndInstallUpdate(update) {
    const baseURL = config.mediaServiceURL.replace(/\/$/, '')
    const downloadURL = new URL(update.download_url, `${baseURL}/`).toString()
    const response = await fetch(downloadURL, { signal: AbortSignal.timeout(10 * 60 * 1000) })
    if (!response.ok) throw new Error(`下载安装包失败 HTTP ${response.status}`)
    const bytes = Buffer.from(await response.arrayBuffer())
    const actualSHA512 = createHash('sha512').update(bytes).digest('base64')
    if (actualSHA512 !== update.sha512) throw new Error('安装包 SHA-512 校验失败')
    const installerPath = join(app.getPath('temp'), update.path)
    writeFileSync(installerPath, bytes)
    const child = spawn(installerPath, [], { detached: true, stdio: 'ignore', windowsHide: false })
    child.unref()
    app.isQuiting = true
    app.quit()
}

async function promptClientUpdate() {
    try {
        const update = await checkClientUpdate()
        if (!update.available) return update
        const result = await dialog.showMessageBox(mainWindow, {
            type: 'info',
            title: '发现新版本',
            message: `千里眼 ${update.version} 已发布`,
            detail: `当前版本：${update.currentVersion}\n是否立即下载并安装更新？`,
            buttons: ['立即更新', '稍后提醒'],
            defaultId: 0,
            cancelId: 1,
            noLink: true
        })
        if (result.response === 0) await downloadAndInstallUpdate(update)
        return update
    } catch (error) {
        console.error('[update] 更新检查失败:', error.message)
        return { available: false, error: error.message, currentVersion: app.getVersion() }
    }
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 760,
        height: 620,
        minWidth: 680,
        minHeight: 520,
        show: false,
        autoHideMenuBar: true,
        icon,
        webPreferences: {
            preload: join(__dirname, '../preload/index.js'),
            contextIsolation: true,
            sandbox: false
        }
    })
    mainWindow.once('ready-to-show', () => {
        if (!startHidden) mainWindow.show()
    })
    mainWindow.on('minimize', (event) => {
        event.preventDefault()
        mainWindow.hide()
    })
    mainWindow.on('close', (event) => {
        if (!app.isQuiting) {
            event.preventDefault()
            mainWindow.hide()
        }
    })
    if (process.env.ELECTRON_RENDERER_URL) mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL)
    else mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
}

function createTray() {
    tray = new Tray(icon)
    tray.setToolTip('千里眼')
    tray.setContextMenu(
        Menu.buildFromTemplate([
            {
                label: '打开状态',
                click: () => showMainPage('status')
            },
            {
                label: 'NVR / RTSP 转推配置',
                click: () => showMainPage('nvr')
            },
            { type: 'separator' },
            { label: '重新推流', click: () => screenController?.restart('tray') }
            // { type: 'separator' },
            // {
            //     label: '退出',
            //     click: () => {
            //         app.isQuiting = true
            //         app.quit()
            //     }
            // }
        ])
    )
    tray.on('click', showMainWindow)
    tray.on('double-click', showMainWindow)
}

ipcMain.handle('app:get-status', async () => ({
    config: {
        mediaServiceURL: config.mediaServiceURL
    },
    system: await collectSystemInfo(),
    stream: screenController?.status() || { running: false, url: '', error: '' }
}))
ipcMain.handle('device:set-user-name', (_event, value) => {
    return saveUserName(value)
})
ipcMain.handle('stream:restart', () => screenController?.restart('ipc'))
ipcMain.handle('nvr:get-config', () => ({
    sources: nvrSources(),
    stream: screenController?.status() || { running: false, sources: [] }
}))
ipcMain.handle('nvr:save-config', (_event, sources) => {
    const apply = () => saveNVRConfiguration(sources)
    streamConfigurationPromise = streamConfigurationPromise.then(apply, apply)
    return streamConfigurationPromise
})
ipcMain.handle('update:check', () => checkClientUpdate())
ipcMain.handle('update:install', (_event, update) => downloadAndInstallUpdate(update))

app.whenReady().then(async () => {
    configureAutoLaunch()
    startHidden =
        process.platform === 'win32' &&
        (app.getLoginItemSettings().wasOpenedAtLogin || process.argv.includes('--hidden'))
    createWindow()
    // Windows 登录启动时保持后台运行，用户可从托盘打开窗口。
    createTray()
    screenController = createScreenController()
    await screenController.start('app_boot')
    setTimeout(promptClientUpdate, 3000)
})

app.on('before-quit', () => {
    app.isQuiting = true
    screenController?.dispose()
})

app.on('window-all-closed', () => {})
