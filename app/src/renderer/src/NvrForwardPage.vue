<script setup>
import { computed, onMounted, ref } from 'vue'

const props = defineProps({
    streamStatus: {
        type: Object,
        default: () => ({ sources: [] })
    }
})
const emit = defineEmits(['back', 'saved'])

const rows = ref([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const message = ref('')
const showAddresses = ref(false)
let localID = 0

function rowKey() {
    localID += 1
    return `local-${Date.now()}-${localID}`
}

function mapSource(source = {}) {
    return {
        _key: source.id || rowKey(),
        id: source.id || '',
        displayName: source.displayName || '',
        url: source.url || '',
        transport: source.transport === 'udp' ? 'udp' : 'tcp',
        enabled: source.enabled !== false
    }
}

const runtimeSources = computed(() => {
    const sources = props.streamStatus?.sources
    return Array.isArray(sources) ? sources : []
})

function runtimeFor(row) {
    return runtimeSources.value.find((source) => source.id === row.id) || {}
}

function addChannel() {
    rows.value.push(
        mapSource({
            displayName: `NVR通道 ${rows.value.length + 1}`,
            transport: 'tcp',
            enabled: true
        })
    )
}

function removeChannel(index) {
    rows.value.splice(index, 1)
}

async function load() {
    loading.value = true
    error.value = ''
    try {
        const result = await window.eyesAPI.getNVRConfig()
        rows.value = (result.sources || []).map(mapSource)
    } catch (loadError) {
        error.value = `读取配置失败：${loadError.message}`
    } finally {
        loading.value = false
    }
}

async function save() {
    error.value = ''
    message.value = ''
    saving.value = true
    try {
        const payload = rows.value.map(({ id, displayName, url, transport, enabled }) => ({
            id,
            displayName: displayName.trim(),
            url: url.trim(),
            transport,
            enabled
        }))
        const result = await window.eyesAPI.saveNVRConfig(payload)
        rows.value = (result.sources || []).map(mapSource)
        message.value = result.result?.ok
            ? '配置已保存，启用的通道正在转推。'
            : `配置已保存，部分通道启动失败：${result.result?.error || '请查看通道状态'}`
        emit('saved', result.stream)
    } catch (saveError) {
        error.value = `保存失败：${saveError.message}`
    } finally {
        saving.value = false
    }
}

onMounted(load)
</script>

<template>
    <section class="nvr-page">
        <div class="page-toolbar">
            <button type="button" class="secondary" @click="emit('back')">返回状态页</button>
            <div class="page-actions">
                <label class="show-address">
                    <input v-model="showAddresses" type="checkbox" />
                    显示完整RTSP地址
                </label>
                <button type="button" :disabled="saving" @click="save">
                    {{ saving ? '正在应用…' : '保存并应用' }}
                </button>
            </div>
        </div>

        <article class="card nvr-intro">
            <span class="label">NVR / RTSP FORWARDING</span>
            <h2>NVR与网络摄像头转推</h2>
            <p>
                填写NVR每个通道或摄像头的RTSP地址。启用后，客户端自动调用FFmpeg，将原始
                H.264/H.265码流直接推送到MediaService，不进行重新编码。
            </p>
            <small>
                推荐使用TCP。RTSP账号和密码保存在本机用户配置中，不会写入运行日志；请限制本机账户权限。
            </small>
        </article>

        <p v-if="loading" class="loading">正在读取NVR配置…</p>
        <p v-if="error" class="form-message error">{{ error }}</p>
        <p v-if="message" class="form-message success">{{ message }}</p>

        <div v-if="!loading" class="channel-list">
            <article v-for="(row, index) in rows" :key="row._key" class="card channel-card">
                <div class="channel-heading">
                    <div>
                        <strong>通道 {{ index + 1 }}</strong>
                        <span
                            :class="['source-chip', runtimeFor(row).running ? 'online' : 'offline']"
                        >
                            {{
                                row.enabled
                                    ? runtimeFor(row).running
                                        ? '正在转推'
                                        : '未连接'
                                    : '已停用'
                            }}
                        </span>
                    </div>
                    <button type="button" class="danger-link" @click="removeChannel(index)">
                        删除
                    </button>
                </div>

                <div class="channel-form">
                    <label>
                        <span>通道名称</span>
                        <input
                            v-model="row.displayName"
                            type="text"
                            maxlength="100"
                            placeholder="例如：厂区东门"
                        />
                    </label>
                    <label>
                        <span>传输方式</span>
                        <select v-model="row.transport">
                            <option value="tcp">TCP（推荐）</option>
                            <option value="udp">UDP</option>
                        </select>
                    </label>
                    <label class="url-field">
                        <span>RTSP地址</span>
                        <input
                            v-model="row.url"
                            :type="showAddresses ? 'text' : 'password'"
                            autocomplete="off"
                            spellcheck="false"
                            placeholder="rtsp://admin:密码@192.168.1.100:554/Streaming/Channels/101"
                        />
                    </label>
                    <label class="enable-field">
                        <input v-model="row.enabled" type="checkbox" />
                        保存后启动该通道
                    </label>
                </div>

                <div v-if="runtimeFor(row).url" class="runtime-url">
                    <span>推流地址</span>
                    <code>{{ runtimeFor(row).url }}</code>
                </div>
                <p v-if="runtimeFor(row).error" class="error channel-error">
                    {{ runtimeFor(row).error }}
                </p>
            </article>

            <button type="button" class="add-channel" @click="addChannel">+ 新增RTSP通道</button>
            <p v-if="rows.length === 0" class="empty-hint">
                尚未配置NVR通道，点击上方按钮添加第一个通道。
            </p>
        </div>
    </section>
</template>

<style scoped>
.nvr-page {
    display: grid;
    gap: 18px;
}
.page-toolbar,
.page-actions,
.channel-heading,
.channel-heading > div {
    display: flex;
    align-items: center;
}
.page-toolbar,
.channel-heading {
    justify-content: space-between;
    gap: 16px;
}
.page-actions,
.channel-heading > div {
    gap: 10px;
}
.secondary,
.add-channel {
    border: 1px solid #355276;
    background: #12253d;
}
.show-address,
.enable-field {
    display: flex;
    align-items: center;
    gap: 7px;
    color: #91a6c1;
    font-size: 12px;
}
.show-address input,
.enable-field input {
    width: auto;
}
.nvr-intro h2 {
    margin: 7px 0 10px;
    font-size: 21px;
}
.nvr-intro p {
    margin: 0 0 9px;
    color: #c6d5e8;
    line-height: 1.7;
}
.channel-list {
    display: grid;
    gap: 14px;
}
.channel-card {
    padding: 18px 20px;
}
.danger-link {
    padding: 5px 9px;
    border: 1px solid #6e3540;
    background: transparent;
    color: #ff9ba5;
    font-size: 12px;
}
.channel-form {
    display: grid;
    grid-template-columns: minmax(220px, 1fr) 160px;
    gap: 13px;
    margin-top: 16px;
}
.channel-form label:not(.enable-field) {
    display: grid;
    gap: 7px;
    color: #91a6c1;
    font-size: 12px;
}
.channel-form input,
.channel-form select {
    min-width: 0;
    width: 100%;
    padding: 10px 11px;
    border: 1px solid #355276;
    border-radius: 8px;
    outline: none;
    background: #07172a;
    color: #f2f7ff;
}
.channel-form input:focus,
.channel-form select:focus {
    border-color: #5da8ff;
    box-shadow: 0 0 0 2px rgba(93, 168, 255, 0.15);
}
.url-field,
.enable-field {
    grid-column: 1 / -1;
}
.runtime-url {
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid #21334c;
}
.runtime-url span {
    display: block;
    margin-bottom: 5px;
    color: #7890ad;
    font-size: 11px;
}
.channel-error {
    margin-bottom: 0;
    font-size: 12px;
}
.add-channel {
    justify-self: start;
}
.empty-hint,
.form-message {
    margin: 0;
    color: #91a6c1;
    font-size: 13px;
}
.form-message {
    padding: 11px 14px;
    border-radius: 9px;
}
.form-message.error {
    background: rgba(255, 101, 119, 0.1);
}
.form-message.success {
    background: rgba(57, 217, 138, 0.1);
    color: #65e5a4;
}
@media (max-width: 720px) {
    .page-toolbar,
    .page-actions {
        align-items: stretch;
        flex-direction: column;
    }
    .channel-form {
        grid-template-columns: 1fr;
    }
    .channel-form > * {
        grid-column: 1;
    }
}
</style>
