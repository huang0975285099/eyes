<script setup>
import { onMounted, onUnmounted, ref } from 'vue'

const loading = ref(true)
const status = ref({ system: {}, registration: {}, stream: {}, config: {} })
let offStream
let offRegistration

async function refresh() {
    status.value = await window.eyesAPI.getStatus()
    loading.value = false
}

async function registerAgain() {
    status.value.registration = await window.eyesAPI.registerDevice()
}

async function restartStream() {
    await window.eyesAPI.restartStream()
    setTimeout(refresh, 1200)
}

function memory(bytes) {
    return bytes ? `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB` : '-'
}

onMounted(async () => {
    await refresh()
    offStream = window.eyesAPI.onStreamStatus((value) => {
        status.value.stream = value
    })
    offRegistration = window.eyesAPI.onRegistration((value) => {
        status.value.registration = value
    })
})

onUnmounted(() => {
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
                    <span class="label">推流目标</span>
                    <strong>{{ status.config.srsHost || '-' }}</strong>
                    <code>{{ status.stream.url || '尚未建立 RTMP 连接' }}</code>
                    <p v-if="status.stream.error" class="error">{{ status.stream.error }}</p>
                </div>
                <button @click="restartStream">重新推流</button>
            </section>

            <section class="grid">
                <article class="card">
                    <div class="card-title">
                        <h2>设备登记</h2>
                        <span :class="['dot', status.registration.ok ? 'ok' : 'bad']"></span>
                    </div>
                    <p>{{ status.registration.message || '尚未登记' }}</p>
                    <small>{{ status.config.recordingServiceURL }}</small>
                    <button class="secondary" @click="registerAgain">重新登记</button>
                </article>
                <article class="card details">
                    <h2>本机信息</h2>
                    <dl>
                        <dt>主机名</dt>
                        <dd>{{ status.system.hostname || '-' }}</dd>
                        <dt>IP 地址</dt>
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
