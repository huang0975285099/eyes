<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import flvjs from 'flv.js'

const loading = ref(true)
const status = ref({ system: {}, registration: {}, stream: {}, config: {} })
const videoRef = ref(null)
const playbackStatus = ref('等待推流')
const playbackError = ref('')
let offStream
let offRegistration
let flvPlayer
let retryTimer

function destroyPlayer() {
    clearTimeout(retryTimer)
    retryTimer = undefined
    if (!flvPlayer) return
    try {
        flvPlayer.pause()
        flvPlayer.unload()
        flvPlayer.detachMediaElement()
        flvPlayer.destroy()
    } catch (error) {
        console.warn('[monitor] 销毁播放器失败:', error)
    }
    flvPlayer = undefined
}

function playbackURL() {
    const rtmpURL = status.value.stream.url
    if (!rtmpURL) return ''
    try {
        const parsed = new URL(rtmpURL)
        const streamName = parsed.pathname.split('/').filter(Boolean).pop()
        if (!streamName) return ''
        return `http://${parsed.hostname}:8090/live/${streamName}.flv`
    } catch {
        return ''
    }
}

async function startPlayback() {
    destroyPlayer()
    playbackError.value = ''
    if (!status.value.stream.running) {
        playbackStatus.value = '推流未启动'
        return
    }
    const url = playbackURL()
    if (!url) {
        playbackStatus.value = '播放地址不可用'
        return
    }
    await nextTick()
    if (!videoRef.value || !flvjs.isSupported()) {
        playbackStatus.value = '当前环境不支持 FLV 播放'
        return
    }
    playbackStatus.value = '正在连接监控画面…'
    const player = flvjs.createPlayer(
        { type: 'flv', isLive: true, url },
        { enableWorker: false, enableStashBuffer: false, stashInitialSize: 128 }
    )
    flvPlayer = player
    player.attachMediaElement(videoRef.value)
    player.on(flvjs.Events.ERROR, (_type, detail) => {
        if (flvPlayer !== player) return
        playbackStatus.value = '画面连接中断'
        playbackError.value = String(detail || '播放失败')
        retryTimer = setTimeout(startPlayback, 3000)
    })
    videoRef.value.addEventListener(
        'playing',
        () => {
            if (flvPlayer === player) playbackStatus.value = '实时画面'
        },
        { once: true }
    )
    player.load()
    player.play().catch(() => {})
}

async function refresh() {
    status.value = await window.eyesAPI.getStatus()
    loading.value = false
}

async function registerAgain() {
    status.value.registration = await window.eyesAPI.registerDevice()
}

async function editUserName() {
    const current = status.value.system.user_name || ''
    const value = window.prompt('请输入姓名或者编号（不超过20个字符）', current)
    if (value === null) return
    const userName = value.trim()
    if ([...userName].length > 20) {
        window.alert('姓名或者编号不能超过20个字符')
        return
    }
    try {
        await window.eyesAPI.setUserName(userName)
        await refresh()
    } catch (error) {
        window.alert(`保存失败：${error.message}`)
    }
}

async function restartStream() {
    await window.eyesAPI.restartStream()
    setTimeout(async () => {
        await refresh()
        startPlayback()
    }, 1200)
}

function memory(bytes) {
    return bytes ? `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB` : '-'
}

onMounted(async () => {
    await refresh()
    startPlayback()
    offStream = window.eyesAPI.onStreamStatus((value) => {
        status.value.stream = value
        startPlayback()
    })
    offRegistration = window.eyesAPI.onRegistration((value) => {
        status.value.registration = value
    })
})

onUnmounted(() => {
    destroyPlayer()
    offStream?.()
    offRegistration?.()
})
</script>

<template>
    <main class="shell">
        <header>
            <div>
                <p class="eyebrow">ALL-SEEING EYES</p>
                <h1>千里眼客户端</h1>
            </div>
            <span :class="['badge', status.stream.running ? 'online' : 'offline']">
                {{ status.stream.running ? '正在推流' : '推流已停止' }}
            </span>
        </header>

        <p v-if="loading" class="loading">正在读取客户端状态…</p>
        <template v-else>
            <section class="card hero">
                <div>
                    <span class="label">RecordingService</span>
                    <strong>{{ status.config.recordingServiceURL || '-' }}</strong>
                    <code>{{ status.stream.url || '尚未建立 RTMP 连接' }}</code>
                    <p v-if="status.stream.error" class="error">{{ status.stream.error }}</p>
                </div>
                <button @click="restartStream">重新推流</button>
            </section>

            <section class="card monitor-card">
                <div class="monitor-heading">
                    <div>
                        <span class="label">MY LIVE VIEW</span>
                        <h2>我的监控画面</h2>
                    </div>
                    <span :class="['badge', playbackStatus === '实时画面' ? 'online' : 'offline']">
                        {{ playbackStatus }}
                    </span>
                </div>
                <div class="video-wrap">
                    <video ref="videoRef" muted autoplay playsinline controls></video>
                    <div v-if="playbackStatus !== '实时画面'" class="video-placeholder">
                        <strong>{{ playbackStatus }}</strong>
                        <small v-if="playbackError">{{ playbackError }}</small>
                    </div>
                </div>
                <p class="monitor-note">仅显示本机当前正在推送的实时画面，播放异常时会自动重连。</p>
            </section>

            <section class="grid">
                <article class="card">
                    <div class="card-title">
                        <h2>设备登记</h2>
                        <span :class="['dot', status.registration.ok ? 'ok' : 'bad']"></span>
                    </div>
                    <p>{{ status.registration.message || '尚未登记' }}</p>
                    <small>{{ status.config.recordingServiceURL }}</small>
                    <div class="user-name-row">
                        当前用户：<strong>{{ status.system.user_name || '无' }}</strong>
                        <button class="link-button" @click="editUserName">修改</button>
                    </div>
                    <small>请输入姓名或者编号，不超过20个字符。</small>
                    <button class="secondary" @click="registerAgain">重新登记</button>
                </article>
                <article class="card details">
                    <h2>本机信息</h2>
                    <dl>
                        <dt>主机名</dt>
                        <dd>{{ status.system.hostname || '-' }}</dd>
                        <dt>公网 IP - 内网 IP</dt>
                        <dd>
                            {{ status.system.public_ip || '-' }} - {{ status.system.ip || '-' }}
                        </dd>
                        <dt>MAC 地址</dt>
                        <dd>{{ status.system.mac || '-' }}</dd>
                        <dt>操作系统</dt>
                        <dd>{{ status.system.os || '-' }}</dd>
                        <dt>处理器</dt>
                        <dd>{{ status.system.cpu || '-' }}</dd>
                        <dt>内存</dt>
                        <dd>{{ memory(status.system.total_memory) }}</dd>
                        <dt>磁盘序列号</dt>
                        <dd>{{ status.system.disk_serial || '-' }}</dd>
                    </dl>
                </article>
            </section>
        </template>
    </main>
</template>
