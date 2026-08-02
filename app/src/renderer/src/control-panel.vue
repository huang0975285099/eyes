<template>
    <div class="monitor-page">
        <div class="monitor-info">
            <el-card class="info-card">
                <template #header>
                    <span class="card-title">设备信息</span>
                </template>
                <el-descriptions :column="2" border>
                    <el-descriptions-item label="本机 IP/MAC">
                        <span class="ip-text">{{ ipAddress }}<br />{{ macAddress }}</span>
                    </el-descriptions-item>
                    <el-descriptions-item label="监控状态">
                        <el-tag
                            :type="monitorStatus === '正常' ? 'success' : 'danger'"
                            size="large"
                        >
                            <el-icon v-if="monitorStatus === '正常'"><CircleCheck /></el-icon>
                            <el-icon v-else><CircleClose /></el-icon>
                            {{ monitorStatus }}
                        </el-tag>
                        <el-button size="small" style="margin-left: 8px" @click="openHistory">
                            历史状态
                        </el-button>
                        <el-button
                            v-if="monitorStatus !== '正常'"
                            size="small"
                            type="warning"
                            style="margin-left: 4px"
                            :loading="monitorReconnecting"
                            @click="manualReconnectMonitor"
                        >
                            手动重连
                        </el-button>
                    </el-descriptions-item>
                    <el-descriptions-item label="用户信息">
                        <span v-if="userInfo.name || userInfo.account">
                            {{ userInfo.name }}
                            <span v-if="userInfo.account" style="color: #909399">
                                （{{ userInfo.account }}）
                            </span>
                        </span>
                        <span v-else style="color: #c0c4cc">未绑定用户</span>
                    </el-descriptions-item>
                    <el-descriptions-item label="远程状态">
                        <el-tag
                            :type="assistStatus === '待机中' ? 'success' : 'danger'"
                            size="large"
                        >
                            <el-icon v-if="assistStatus === '待机中'"><Connection /></el-icon>
                            <el-icon v-else><CircleClose /></el-icon>
                            {{ assistStatus }}
                        </el-tag>
                        <el-button size="small" style="margin-left: 8px" @click="openAssistHistory">
                            历史状态
                        </el-button>
                        <el-button
                            v-if="assistStatus === '异常'"
                            size="small"
                            type="warning"
                            style="margin-left: 4px"
                            :loading="assistReconnecting"
                            @click="manualReconnectAssist"
                        >
                            手动重连
                        </el-button>
                    </el-descriptions-item>
                    <el-descriptions-item label="本机服务">
                        <el-tag :type="wsTagType" size="large">
                            <el-icon v-if="wsInfo.status === 'connected'"><CircleCheck /></el-icon>
                            <el-icon v-else><CircleClose /></el-icon>
                            {{ wsLabel }}
                        </el-tag>
                        <el-button
                            v-if="wsInfo.status !== 'connected'"
                            size="small"
                            type="warning"
                            style="margin-left: 8px"
                            :loading="wsReconnecting"
                            @click="manualReconnectWs"
                        >
                            手动重连
                        </el-button>
                        <span
                            v-if="wsInfo.error"
                            style="margin-left: 8px; font-size: 12px; color: #e6a23c"
                        >
                            {{ wsInfo.error }}
                        </span>
                    </el-descriptions-item>
                    <el-descriptions-item label="推流录制">
                        <el-tag :type="rtmpPushActive ? 'success' : 'info'" size="large">
                            {{ rtmpPushActive ? '推流中' : '未推流' }}
                        </el-tag>
                        <span
                            v-if="rtmpPushUrl"
                            style="margin-left: 8px; font-size: 12px; color: #909399"
                        >
                            {{ rtmpPushUrl }}
                        </span>
                    </el-descriptions-item>
                </el-descriptions>

                <!-- 推流异常诊断提示：状态异常时展示，帮助快速定位原因 -->
                <el-alert
                    v-if="monitorStatus === '异常' && macAddress && macAddress !== '获取中...'"
                    type="warning"
                    title="推流异常 — 常见原因排查"
                    :closable="false"
                    show-icon
                    style="margin-top: 14px"
                >
                    <template #default>
                        <div style="line-height: 2; font-size: 13px">
                            <div>
                                ① 主服务器的RTMP服务未运行，或您当前设备无法连接流媒体服务器的端口
                            </div>
                            <!-- <div>
                                ② 本机 MAC 地址
                                <el-tag size="small" type="info" style="font-family: monospace; letter-spacing: 1px">{{ macAddress }}</el-tag>
                                未在管理系统注册 — 请联系管理员将此设备添加到「电脑管理」后重试
                            </div> -->
                        </div>
                    </template>
                </el-alert>
            </el-card>
        </div>

        <div class="monitor-screen">
            <el-card class="screen-card">
                <template #header>
                    <div class="screen-card-header">
                        <span class="card-title">实时</span>
                        <span v-if="isCapturing" class="admin-hint"> 推流中 </span>
                    </div>
                </template>
                <div
                    class="screen-container"
                    v-loading="loading"
                    element-loading-text="正在启动采集..."
                >
                    <video
                        ref="videoRef"
                        class="screen-canvas"
                        v-show="isCapturing"
                        muted
                        autoplay
                        playsinline
                    />
                    <div class="screen-placeholder" v-show="!isCapturing">
                        <el-icon class="placeholder-icon"><Monitor /></el-icon>
                        <p>
                            {{
                                monitorStatus === '异常' || monitorStatus === '已断开'
                                    ? '监控已中断，正在重连...'
                                    : '监控启动中...'
                            }}
                        </p>
                    </div>
                </div>
            </el-card>
        </div>

        <el-dialog v-model="assistHistoryVisible" title="远程协助状态历史" width="980px" top="6vh">
            <el-table
                v-loading="assistHistoryLoading"
                :data="assistHistoryData"
                stripe
                border
                size="small"
                style="width: 100%"
            >
                <el-table-column label="状态" width="90">
                    <template #default="{ row }">
                        <el-tag :type="historyTagType(row.status)" size="small">
                            {{ row.status }}
                        </el-tag>
                    </template>
                </el-table-column>
                <el-table-column label="原因" width="140">
                    <template #default="{ row }">{{ assistReasonLabel(row.reason) }}</template>
                </el-table-column>
                <el-table-column label="详情" min-width="280" show-overflow-tooltip>
                    <template #default="{ row }">{{ row.detail || '-' }}</template>
                </el-table-column>
                <el-table-column label="来源" width="80" align="center">
                    <template #default="{ row }">
                        <el-tag :type="row.source === 'main' ? 'warning' : 'info'" size="small">
                            {{ row.source || '-' }}
                        </el-tag>
                    </template>
                </el-table-column>
                <el-table-column label="累计启动" width="85" align="center">
                    <template #default="{ row }">{{ row.restartTotal ?? '-' }}</template>
                </el-table-column>
                <el-table-column label="时间" width="160">
                    <template #default="{ row }">{{ fmtDatetime(row.created_at) }}</template>
                </el-table-column>
            </el-table>
            <el-pagination
                style="margin-top: 12px; justify-content: flex-end; display: flex"
                :current-page="assistHistoryPage"
                :page-size="historyPageSize"
                :total="assistHistoryTotal"
                layout="total, prev, pager, next"
                small
                @current-change="
                    (p) => {
                        assistHistoryPage = p
                        loadAssistHistory()
                    }
                "
            />
        </el-dialog>

        <el-dialog v-model="historyVisible" title="监控状态历史" width="980px" top="6vh">
            <el-table
                v-loading="historyLoading"
                :data="historyData"
                stripe
                border
                size="small"
                style="width: 100%"
            >
                <el-table-column label="状态" width="90">
                    <template #default="{ row }">
                        <el-tag :type="historyTagType(row.status)" size="small">
                            {{ row.status }}
                        </el-tag>
                    </template>
                </el-table-column>
                <el-table-column label="原因" width="140">
                    <template #default="{ row }">{{ historyReasonLabel(row.reason) }}</template>
                </el-table-column>
                <el-table-column label="详情" min-width="280" show-overflow-tooltip>
                    <template #default="{ row }">{{ row.detail || '-' }}</template>
                </el-table-column>
                <el-table-column label="来源" width="80" align="center">
                    <template #default="{ row }">
                        <el-tag :type="row.source === 'main' ? 'warning' : 'info'" size="small">
                            {{ row.source || '-' }}
                        </el-tag>
                    </template>
                </el-table-column>
                <el-table-column label="累计启动" width="85" align="center">
                    <template #default="{ row }">{{ row.restartTotal ?? '-' }}</template>
                </el-table-column>
                <el-table-column label="时间" width="160">
                    <template #default="{ row }">{{ fmtDatetime(row.created_at) }}</template>
                </el-table-column>
            </el-table>
            <el-pagination
                style="margin-top: 12px; justify-content: flex-end; display: flex"
                :current-page="historyPage"
                :page-size="historyPageSize"
                :total="historyTotal"
                layout="total, prev, pager, next"
                small
                @current-change="
                    (p) => {
                        historyPage = p
                        loadHistory()
                    }
                "
            />
        </el-dialog>
    </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { getPreferredNIC } from '../composables/useAdminApi'
import mpegts from 'mpegts.js'

const videoRef = ref(null)
const ipAddress = ref('获取中...')
const macAddress = ref('获取中...')
const hostname = ref('获取中...')
const userInfo = ref({ name: '', account: '' })
let _networkInfoReady = null
const monitorStatus = ref('未启动')
const isCapturing = ref(false)
const loading = ref(false)
const rtmpPushActive = ref(false)
const rtmpPushUrl = ref('')
const srsHttpPort = ref('28080')

const assistStatus = ref('待机中')
const monitorReconnecting = ref(false)
const assistReconnecting = ref(false)
const wsReconnecting = ref(false)

// WS 服务连接状态
const wsInfo = ref({ status: 'connecting', url: '', port: null, reconnectCount: 0, error: null })
const wsTagType = computed(() => {
    switch (wsInfo.value.status) {
        case 'connected':
            return 'success'
        case 'connecting':
            return 'primary'
        case 'disconnected':
            return 'warning'
        case 'failed':
            return 'danger'
        default:
            return 'info'
    }
})
const wsLabel = computed(() => {
    switch (wsInfo.value.status) {
        case 'connected':
            return '正常'
        case 'connecting':
            return '连接中'
        case 'disconnected':
            return `断开（第 ${wsInfo.value.reconnectCount} 次重连）`
        case 'failed':
            return '连接失败'
        default:
            return '未知'
    }
})

let pushStatusTimer = null
let flvPlayer = null
let retryTimer = null
let unsubDied = null
let unsubResumed = null
let unsubLocked = null
let unsubAssistDied = null
let unsubAssistStarted = null
let unsubWsStatus = null

let isScreenLocked = false

async function loadPushStatus() {
    try {
        const { running, rtmpUrl } = await window.electronAPI.screenHelperMonitorStatus()
        rtmpPushActive.value = running
        rtmpPushUrl.value = rtmpUrl || ''
    } catch (error) {
        console.warn('[control-panel] 读取推流状态失败:', error)
    }
}

function destroyFlvPlayer() {
    if (!flvPlayer) return
    try {
        flvPlayer.pause()
        flvPlayer.unload()
        flvPlayer.detachMediaElement()
        flvPlayer.destroy()
    } catch (error) {
        console.warn('[control-panel] 销毁播放器失败:', error)
    }
    flvPlayer = null
}

function buildFlvUrl() {
    const url = rtmpPushUrl.value
    if (!url) return null
    try {
        const rtmpUrl = new URL(url)
        const streamName = rtmpUrl.pathname.split('/').pop()
        if (!streamName) return null
        return `http://${rtmpUrl.hostname}:${srsHttpPort.value}/live/${streamName}.flv`
    } catch {
        return null
    }
}

async function startFlvPlayback() {
    destroyFlvPlayer()
    const flvUrl = buildFlvUrl()
    if (!flvUrl) {
        monitorStatus.value = '未推流'
        loading.value = false
        return
    }

    const video = videoRef.value
    if (!video) return

    loading.value = true
    try {
        const player = mpegts.createPlayer(
            { type: 'flv', isLive: true, url: flvUrl },
            { enableWorker: false, enableStashBuffer: false, stashInitialSize: 128 }
        )
        player.attachMediaElement(video)
        player.load()
        flvPlayer = player

        player.on(mpegts.Events.ERROR, () => {
            if (flvPlayer !== player) return
            destroyFlvPlayer()
            isCapturing.value = false
            monitorStatus.value = '异常'
            loading.value = false
            scheduleRetry(5000)
        })

        video.addEventListener('playing', function onPlaying() {
            if (flvPlayer !== player) return
            isCapturing.value = true
            monitorStatus.value = '正常'
            loading.value = false
            video.removeEventListener('playing', onPlaying)
        })

        video.play().catch(() => {
            video.muted = true
            video.play().catch(() => {})
        })
    } catch (err) {
        console.error('启动 FLV 播放失败:', err)
        monitorStatus.value = '异常'
        loading.value = false
        scheduleRetry(5000)
    }
}

async function loadNetworkInfo() {
    try {
        const nic = await getPreferredNIC()
        ipAddress.value = nic.ip || '未检测到'
        macAddress.value = nic.mac || '未检测到'
    } catch {
        ipAddress.value = '获取失败'
        macAddress.value = '获取失败'
    }

    try {
        const sysInfo = await window.electronAPI.getSystemInfo()
        hostname.value = sysInfo.hostname || '未知'
        if (sysInfo.srsHttpPort) srsHttpPort.value = sysInfo.srsHttpPort
    } catch {
        hostname.value = '未知'
    }
}

async function loadUserInfo() {
    // 第一步：从 IPC 取已登录用户信息（login/login2 流程写入）
    try {
        const data = await window.electronAPI.getCurrentUserInfomation()
        const u = data?.user ?? data ?? {}
        const name = u.name || u.realName || ''
        const account = u.account || u.username || ''
        if (name || account) {
            userInfo.value = { name, account }
            return
        }
    } catch (e) {
        console.warn('[userInfo] IPC 获取失败:', e?.message)
    }

    // 第二步：未登录时，用本机 MAC/IP 去后端公开接口查绑定的用户信息
    try {
        await _networkInfoReady // 等待网络信息加载完成
        const mac = macAddress.value
        const ip = ipAddress.value
        if (!mac || mac === '获取中...' || mac === '获取失败') return

        const safeIp = ip && ip !== '获取中...' && ip !== '获取失败' ? ip : ''
        // 走主进程发起（服务端已强制 X-Client-Key，密钥只在主进程）
        const d = (await window.electronAPI.getComputerUserInfo({ mac, ip: safeIp })) ?? {}
        if (d.registered) {
            userInfo.value = { name: d.name || '', account: d.account || '' }
        }
    } catch (e) {
        console.warn('[userInfo] API 查询失败:', e?.message)
    }
}

const historyVisible = ref(false)
const historyLoading = ref(false)
const historyData = ref([])
const historyTotal = ref(0)
let historyPage = 1
const historyPageSize = 20

const assistHistoryVisible = ref(false)
const assistHistoryLoading = ref(false)
const assistHistoryData = ref([])
const assistHistoryTotal = ref(0)
let assistHistoryPage = 1

const REASON_LABELS = {
    connected: '连接成功',
    ws_disconnected: '播放断开',
    ws_error: '播放错误',
    start_failed: '启动失败',
    helper_crashed: '监控进程崩溃',
    manual_stop: '手动停止',
    system_resumed: '系统解锁/恢复',
    helper_started: '进程已启动',
    helper_spawn_failed: '进程拉起失败',
    helper_exited: '进程退出',
    helper_user_stop: '用户停止',
    helper_user_stop_request: '请求停止',
    helper_not_found: 'helper.exe 不存在',
    helper_lock_screen: '屏幕锁定',
    helper_resume_restart: '解锁/恢复重启',
    helper_resume_restart_failed: '解锁后重启失败',
    helper_app_quit: '应用退出'
}

function historyReasonLabel(reason) {
    return REASON_LABELS[reason] || reason || '-'
}

const ASSIST_REASON_LABELS = {
    assist_started: '服务启动',
    assist_crashed: '服务崩溃',
    assist_stopped: '服务停止',
    assist_spawn_failed: '进程拉起失败',
    assist_exited: '进程退出',
    assist_user_stop: '用户停止',
    assist_user_stop_request: '请求停止',
    assist_user_restart: '用户手动重连',
    assist_not_found: 'helper.exe 不存在',
    assist_auto_restart: '自动重启',
    assist_auto_restart_failed: '自动重启失败',
    assist_restart_giveup: '放弃自动重启',
    assist_resume_restart: '解锁/恢复重启',
    assist_resume_restart_failed: '解锁后重启失败',
    assist_app_quit: '应用退出'
}

function assistReasonLabel(reason) {
    return ASSIST_REASON_LABELS[reason] || reason || '-'
}

async function logAssistStatus(status, reason, detail = '') {
    await _networkInfoReady?.catch(() => {})
    window.electronAPI
        .monitorLogEvent({
            status,
            reason,
            detail,
            source: 'renderer',
            ip: ipAddress.value,
            mac: macAddress.value,
            hostname: hostname.value,
            created_at: localISOString()
        })
        .catch(() => {})
}

async function loadAssistHistory() {
    assistHistoryLoading.value = true
    try {
        const res = await window.electronAPI.monitorGetEvents({
            page: assistHistoryPage,
            pageSize: historyPageSize,
            kind: 'assist'
        })
        assistHistoryData.value = res?.list || []
        assistHistoryTotal.value = res?.total || 0
    } catch (e) {
        console.error('获取远程协助历史失败:', e)
    } finally {
        assistHistoryLoading.value = false
    }
}

function openAssistHistory() {
    assistHistoryVisible.value = true
    assistHistoryPage = 1
    loadAssistHistory()
}

function historyTagType(status) {
    if (status === '正常' || status === '待机中') return 'success'
    if (status === '异常') return 'danger'
    if (status === '已断开') return 'warning'
    if (status === '恢复中') return 'primary'
    if (status === '停止中') return 'warning'
    if (status === '已停止') return 'info'
    return 'info'
}

function fmtDatetime(val) {
    if (!val) return '-'
    const d = new Date(val)
    const p = (n) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function localISOString() {
    const d = new Date()
    const p2 = (n) => String(n).padStart(2, '0')
    const p3 = (n) => String(n).padStart(3, '0')
    const offset = -d.getTimezoneOffset()
    const sign = offset >= 0 ? '+' : '-'
    const abs = Math.abs(offset)
    return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}T${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}.${p3(d.getMilliseconds())}${sign}${p2(Math.floor(abs / 60))}:${p2(abs % 60)}`
}

async function loadHistory() {
    historyLoading.value = true
    try {
        const res = await window.electronAPI.monitorGetEvents({
            page: historyPage,
            pageSize: historyPageSize,
            kind: 'monitor'
        })
        historyData.value = res?.list || []
        historyTotal.value = res?.total || 0
    } catch (e) {
        console.error('获取历史状态失败:', e)
    } finally {
        historyLoading.value = false
    }
}

function openHistory() {
    historyVisible.value = true
    historyPage = 1
    loadHistory()
}

async function logStatus(status, reason, detail = '') {
    await _networkInfoReady?.catch(() => {})
    window.electronAPI
        .monitorLogEvent({
            status,
            reason,
            detail,
            source: 'renderer',
            ip: ipAddress.value,
            mac: macAddress.value,
            hostname: hostname.value,
            created_at: localISOString()
        })
        .catch(() => {})
}

function scheduleRetry(delayMs = 3000) {
    if (isScreenLocked) return
    clearTimeout(retryTimer)
    retryTimer = setTimeout(async () => {
        retryTimer = null
        if (!isCapturing.value && !isScreenLocked) {
            await loadPushStatus()
            startFlvPlayback()
        }
    }, delayMs)
}

async function manualReconnectMonitor() {
    if (monitorReconnecting.value) return
    monitorReconnecting.value = true
    clearTimeout(retryTimer)
    retryTimer = null
    destroyFlvPlayer()
    isCapturing.value = false
    await loadPushStatus()
    await startFlvPlayback()
    monitorReconnecting.value = false
}

async function manualReconnectWs() {
    if (wsReconnecting.value) return
    wsReconnecting.value = true
    try {
        await window.electronAPI.wsReconnect()
    } catch (e) {
        console.error('WS 手动重连失败:', e)
    } finally {
        wsReconnecting.value = false
    }
}

async function manualReconnectAssist() {
    if (assistReconnecting.value) return
    assistReconnecting.value = true
    try {
        await window.electronAPI.screenHelperAssistRestart()
    } catch (e) {
        console.error('远程协助重连失败:', e)
    } finally {
        assistReconnecting.value = false
    }
}

function stopCapture() {
    clearTimeout(retryTimer)
    retryTimer = null
    isCapturing.value = false
    destroyFlvPlayer()
    monitorStatus.value = '已停止'
    logStatus('已停止', 'manual_stop', '用户点击停止采集按钮')
}

onMounted(async () => {
    _networkInfoReady = loadNetworkInfo()
    loadUserInfo()
    await loadPushStatus()
    startFlvPlayback()

    // WS 服务状态：先取一次快照，再订阅后续变化
    window.electronAPI
        .wsStatus()
        .then((s) => {
            if (s) wsInfo.value = s
        })
        .catch(() => {})
    unsubWsStatus = window.electronAPI.onWsStatusChanged((s) => {
        wsInfo.value = s
    })

    window.electronAPI
        .screenHelperAssistStatus()
        .then(({ running }) => {
            assistStatus.value = running ? '待机中' : '异常'
            logAssistStatus(
                running ? '待机中' : '异常',
                running ? 'assist_started' : 'assist_crashed',
                `渲染器启动时查询 assist 状态: running=${running}`
            )
        })
        .catch(() => {})

    unsubAssistDied = window.electronAPI.onScreenHelperAssistDied((info) => {
        // 用户主动停止 / 解锁强制重启 / 旧进程被新进程接管：不是崩溃，紧跟会有 assist-started
        if (info && info.intended) {
            assistStatus.value = '已停止'
            logAssistStatus(
                '已停止',
                'assist_stopped',
                `收到主进程 assist-died 事件（intended=${info.reason || true}）`
            )
            return
        }
        assistStatus.value = '异常'
        logAssistStatus('异常', 'assist_crashed', '收到主进程 assist-died 事件')
    })
    unsubAssistStarted = window.electronAPI.onScreenHelperAssistStarted(() => {
        assistStatus.value = '待机中'
        logAssistStatus('待机中', 'assist_started', '收到主进程 assist-started 事件')
    })

    unsubDied = window.electronAPI.onScreenHelperDied(() => {
        monitorStatus.value = '异常'
        isCapturing.value = false
        rtmpPushActive.value = false
        destroyFlvPlayer()
        logStatus('异常', 'helper_crashed', '收到主进程 screen-helper:died 事件')
        scheduleRetry(3000)
    })

    unsubLocked = window.electronAPI.onScreenHelperLocked(() => {
        isScreenLocked = true
        clearTimeout(retryTimer)
        retryTimer = null
        monitorStatus.value = '已停止'
        logStatus('已停止', 'helper_lock_screen', '屏幕锁定，主进程通知暂停监控')
    })

    unsubResumed = window.electronAPI.onScreenHelperResumed(() => {
        isScreenLocked = false
        loadPushStatus()
        if (isCapturing.value) return
        clearTimeout(retryTimer)
        retryTimer = null
        logStatus('恢复中', 'system_resumed', '收到主进程 resumed 事件，重新连接播放')
        destroyFlvPlayer()
        isCapturing.value = false
        startFlvPlayback()
    })

    pushStatusTimer = setInterval(loadPushStatus, 10000)
})

onUnmounted(() => {
    unsubDied?.()
    unsubResumed?.()
    unsubLocked?.()
    unsubAssistDied?.()
    unsubAssistStarted?.()
    unsubWsStatus?.()
    clearInterval(pushStatusTimer)
    clearTimeout(retryTimer)
    stopCapture()
})
</script>

<style scoped>
.monitor-page {
    width: 100%;
    min-height: 100vh;
    padding: 24px;
    box-sizing: border-box;
}

.monitor-info {
    max-width: 900px;
    margin: 0 auto 20px;
}

.info-card {
    border-radius: 8px;
}

.card-title {
    font-weight: 600;
    font-size: 16px;
}

.ip-text {
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 15px;
    font-weight: 600;
    color: #409eff;
}

.path-text {
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 13px;
    color: #606266;
    word-break: break-all;
}

.monitor-screen {
    max-width: 900px;
    margin: 0 auto;
}

.screen-card {
    border-radius: 8px;
}

.screen-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
}

.admin-hint {
    font-size: 12px;
    color: #909399;
    flex: 1;
}

.screen-container {
    width: 100%;
    min-height: 400px;
    background: #1a1a2e;
    border-radius: 6px;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
}

.screen-canvas {
    width: 100%;
    height: auto;
    max-height: 500px;
    object-fit: contain;
    display: block;
}

.screen-placeholder {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: #606266;
    padding: 60px 0;
}

.placeholder-icon {
    font-size: 64px;
    color: #909399;
    margin-bottom: 16px;
}

.screen-placeholder p {
    font-size: 14px;
    color: #909399;
    margin: 0;
}
</style>
