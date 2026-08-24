<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import flvjs from 'flv.js'

const loading = ref(true)
const status = ref({ system: {}, stream: {}, config: {} })
const videoRef = ref(null)
const selectedSourceID = ref('')
const playbackStatus = ref('等待推流')
const playbackError = ref('')
const editingUserName = ref(false)
const userNameInput = ref('')
const userNameError = ref('')
const userNameSaving = ref(false)
const userNameInputRef = ref(null)
let offStream
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

function streamSources() {
    const sources = status.value.stream.sources
    if (Array.isArray(sources) && sources.length) return sources
    return [
        {
            id: 'desktop',
            type: 'screen',
            displayName: '电脑桌面',
            running: status.value.stream.running,
            url: status.value.stream.url,
            error: status.value.stream.error
        }
    ]
}

function ensureSelectedSource() {
    const sources = streamSources()
    if (!sources.some((source) => source.id === selectedSourceID.value)) {
        selectedSourceID.value =
            sources.find((source) => source.running)?.id || sources[0]?.id || ''
    }
}

function selectedSource() {
    ensureSelectedSource()
    return streamSources().find((source) => source.id === selectedSourceID.value) || {}
}

function playbackURL() {
    const rtmpURL = selectedSource().url
    if (!rtmpURL) return ''
    try {
        const parsed = new URL(rtmpURL)
        const streamName = parsed.pathname.split('/').filter(Boolean).pop()
        if (!streamName) return ''
        return `http://${parsed.hostname}:8080/live/${streamName}.flv`
    } catch {
        return ''
    }
}

async function startPlayback() {
    destroyPlayer()
    playbackError.value = ''
    const source = selectedSource()
    if (!source.running) {
        playbackStatus.value = '推流未启动'
        playbackError.value = source.error || ''
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
    ensureSelectedSource()
    loading.value = false
}

async function editUserName() {
    userNameInput.value = status.value.system.user_name || ''
    userNameError.value = ''
    editingUserName.value = true
    await nextTick()
    userNameInputRef.value?.focus()
    userNameInputRef.value?.select()
}

function cancelEditUserName() {
    editingUserName.value = false
    userNameError.value = ''
}

async function saveUserName() {
    const userName = userNameInput.value.trim()
    if (!userName) {
        userNameError.value = '请输入姓名或者编号'
        return
    }
    if ([...userName].length > 20) {
        userNameError.value = '姓名或者编号不能超过20个字符'
        return
    }
    userNameSaving.value = true
    userNameError.value = ''
    try {
        await window.eyesAPI.setUserName(userName)
        await refresh()
        editingUserName.value = false
    } catch (error) {
        userNameError.value = `保存失败：${error.message}`
    } finally {
        userNameSaving.value = false
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
        ensureSelectedSource()
        startPlayback()
    })
})

onUnmounted(() => {
    destroyPlayer()
    offStream?.()
})
</script>

<template>
    <main class="shell">
        <header>
            <div>
                <p class="eyebrow">ALL-SEEING EYES</p>
                <h1>千里眼客户端</h1>
                <p class="app-version">当前版本 v{{ status.system.app_version || '-' }}</p>
            </div>
            <span :class="['badge', status.stream.running ? 'online' : 'offline']">
                {{ status.stream.running ? '正在推流' : '推流已停止' }}
            </span>
        </header>

        <p v-if="loading" class="loading">正在读取客户端状态…</p>
        <template v-else>
            <section class="card hero">
                <div>
                    <span class="label">MediaService</span>
                    <strong>{{ status.config.mediaServiceURL || '-' }}</strong>
                    <code>{{ selectedSource().url || '尚未建立 RTMP 连接' }}</code>
                    <p v-if="status.stream.error" class="error">{{ status.stream.error }}</p>
                    <div class="source-status-list">
                        <span
                            v-for="source in streamSources()"
                            :key="source.id"
                            :class="['source-chip', source.running ? 'online' : 'offline']"
                        >
                            {{ source.displayName }} · {{ source.running ? '在线' : '离线' }}
                        </span>
                    </div>
                </div>
                <button @click="restartStream">重新推流</button>
            </section>

            <section class="card monitor-card">
                <div class="monitor-heading">
                    <div>
                        <span class="label">MY LIVE VIEW</span>
                        <h2>我的监控画面</h2>
                    </div>
                    <div class="monitor-controls">
                        <select
                            v-if="streamSources().length > 1"
                            v-model="selectedSourceID"
                            @change="startPlayback"
                        >
                            <option
                                v-for="source in streamSources()"
                                :key="source.id"
                                :value="source.id"
                            >
                                {{ source.displayName }}
                            </option>
                        </select>
                        <span
                            :class="['badge', playbackStatus === '实时画面' ? 'online' : 'offline']"
                        >
                            {{ playbackStatus }}
                        </span>
                    </div>
                </div>
                <div class="video-wrap">
                    <video ref="videoRef" muted autoplay playsinline controls></video>
                    <div v-if="playbackStatus !== '实时画面'" class="video-placeholder">
                        <strong>{{ playbackStatus }}</strong>
                        <small v-if="playbackError">{{ playbackError }}</small>
                    </div>
                </div>
                <p class="monitor-note">可切换查看本机各视频源，播放异常时会自动重连。</p>
            </section>

            <section class="grid">
                <article class="card details">
                    <h2>本机信息</h2>
                    <dl>
                        <dt>当前用户</dt>
                        <dd class="user-name-cell">
                            <template v-if="!editingUserName">
                                <span>{{ status.system.user_name || '无' }}</span>
                                <button type="button" class="link-button" @click="editUserName">
                                    修改
                                </button>
                            </template>
                            <form v-else class="user-name-form" @submit.prevent="saveUserName">
                                <input
                                    ref="userNameInputRef"
                                    v-model="userNameInput"
                                    type="text"
                                    maxlength="20"
                                    placeholder="请输入姓名或者编号"
                                    :disabled="userNameSaving"
                                    @keydown.esc.prevent="cancelEditUserName"
                                />
                                <button
                                    type="submit"
                                    class="link-button"
                                    :disabled="userNameSaving"
                                >
                                    {{ userNameSaving ? '保存中…' : '保存' }}
                                </button>
                                <button
                                    type="button"
                                    class="link-button muted"
                                    :disabled="userNameSaving"
                                    @click="cancelEditUserName"
                                >
                                    取消
                                </button>
                            </form>
                            <small v-if="userNameError" class="field-error">
                                {{ userNameError }}
                            </small>
                        </dd>
                        <dt>主机名</dt>
                        <dd>{{ status.system.hostname || '-' }}</dd>
                        <dt>内网 IP</dt>
                        <dd>{{ status.system.ip || '-' }}</dd>
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
