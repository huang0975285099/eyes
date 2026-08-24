import { app, BrowserWindow, ipcMain, Tray, Menu, dialog } from 'electron'
import { execFile, spawn } from 'child_process'
import { createHash } from 'crypto'
import { existsSync, readFileSync, writeFileSync } from 'fs'
import os from 'os'
import { join } from 'path'
import icon from '../../resources/icon.png?asset'
import { setupScreenHelper } from './screen-helper-main'
import { getPreferredNICAsync } from './network-util'

const DEFAULT_CONFIG = {
    mediaServiceURL: 'http://10.0.20.219:22222',
    apiKey: 'Yx7pK4vN9mQ2tR8wF6cH3sD5jL1aZ0eB',
    userName: '',
    videoSources: [{ id: 'desktop', type: 'screen', displayName: '电脑桌面', enabled: true }]
}

let mainWindow
let tray
let screenController
let registerTimer
let lastRegistration = { ok: false, message: '尚未登记', at: null }
let startHidden = false
let publicIP = ''

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
        public_ip: publicIP,
        app_version: app.getVersion()
    }
}

function saveUserName(value) {
    const userName = String(value || '').trim()
    if ([...userName].length > 20) throw new Error('姓名或编号不能超过20个字符')
    config.userName = userName
    const file = join(app.getPath('userData'), 'config.json')
    let userConfig = {}
    try {
        if (existsSync(file)) userConfig = JSON.parse(readFileSync(file, 'utf8'))
    } catch (error) {
        console.error(`[config] 保留用户配置失败 ${file}:`, error.message)
    }
    writeFileSync(file, JSON.stringify({ ...userConfig, userName }, null, 2), 'utf8')
    return userName
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

async function registerDevice() {
    if (!config.mediaServiceURL) {
        lastRegistration = {
            ok: false,
            message: '未配置 mediaServiceURL',
            at: new Date().toISOString()
        }
        return lastRegistration
    }
    try {
        const info = await collectSystemInfo()
        if (!info.mac) throw new Error('未找到可用网卡 MAC 地址')
        // public_ip 由服务端根据连接来源判定，只用于本地状态展示，不能随登记请求提交。
        const registrationPayload = { ...info }
        delete registrationPayload.public_ip
        const response = await fetch(
            `${config.mediaServiceURL.replace(/\/$/, '')}/api/clients/register`,
            {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Client-Key': config.apiKey || ''
                },
                body: JSON.stringify(registrationPayload),
                signal: AbortSignal.timeout(8000)
            }
        )
        const body = await response.json().catch(() => ({}))
        if (!response.ok) throw new Error(body.message || `HTTP ${response.status}`)
        publicIP = body.public_ip || ''
        info.public_ip = publicIP
        lastRegistration = {
            ok: true,
            message: '设备信息已登记',
            at: new Date().toISOString(),
            info
        }
    } catch (error) {
        lastRegistration = { ok: false, message: error.message, at: new Date().toISOString() }
        console.error('[device] 登记失败:', error.message)
    }
    mainWindow?.webContents.send('device:registration-changed', lastRegistration)
    return lastRegistration
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
                click: showMainWindow
            },
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
    registration: lastRegistration,
    stream: screenController?.status() || { running: false, url: '', error: '' }
}))
ipcMain.handle('device:register', () => registerDevice())
ipcMain.handle('device:set-user-name', (_event, value) => {
    const userName = saveUserName(value)
    return registerDevice().then(() => userName)
})
ipcMain.handle('stream:restart', () => screenController?.restart('ipc'))
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
    screenController = setupScreenHelper({
        mediaServiceURL: config.mediaServiceURL,
        clientApiKey: config.apiKey,
        videoSources: config.videoSources,
        onStatus: (status) => mainWindow?.webContents.send('stream:status-changed', status)
    })
    await registerDevice()
    await screenController.start('app_boot')
    registerTimer = setInterval(registerDevice, 5 * 60 * 1000)
    setTimeout(promptClientUpdate, 3000)
})

app.on('before-quit', () => {
    app.isQuiting = true
    if (registerTimer) clearInterval(registerTimer)
    screenController?.stop('app_quit')
})

app.on('window-all-closed', () => {})
