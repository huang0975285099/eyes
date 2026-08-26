<template>
  <q-card dark class="device-detail-shell">
    <q-toolbar class="device-detail-header">
      <q-btn flat round dense icon="arrow_back" aria-label="返回设备列表" @click="emit('close')" />
      <q-toolbar-title>
        <small>点位详情</small>
        <strong>{{ source.display_name || source.stream_name }}</strong>
      </q-toolbar-title>
      <q-btn flat round dense icon="refresh" aria-label="刷新详情" :loading="busy" @click="loadResults" />
    </q-toolbar>

    <q-tabs v-model="tab" dense no-caps align="justify" active-color="primary" indicator-color="primary" class="device-detail-tabs">
      <q-tab name="live" icon="smart_display" label="实时" />
      <q-tab name="frames" icon="photo_library" label="抽帧" />
      <q-tab name="recordings" icon="video_library" label="录像" />
      <q-tab name="services" icon="tune" label="服务" />
    </q-tabs>

    <q-tab-panels v-model="tab" animated dark keep-alive class="device-detail-panels">
      <q-tab-panel name="live" class="detail-live-panel">
        <LiveFeed :source="source" :active="tab === 'live'" hint="" />
      </q-tab-panel>

      <q-tab-panel name="frames" class="detail-scroll-panel">
        <div class="detail-section-heading">
          <div><p>FRAME RESULTS</p><h2>实时抽帧结果</h2></div>
          <q-badge color="primary" text-color="dark">{{ frames.length }} 张</q-badge>
        </div>
        <div v-if="frames.length" class="frame-result-grid">
          <button v-for="frame in frames" :key="frame.id" type="button" class="frame-result-card" @click="previewFrame = frameURL(frame.id)">
            <img :src="frameURL(frame.id)" loading="lazy" alt="抽帧图片" />
            <span>{{ localTime(frame.captured_at) }}</span>
            <small>{{ fileSize(frame.file_size) }}</small>
          </button>
        </div>
        <EmptyState v-else icon="photo_library" title="暂无抽帧结果" description="点位保持在线后，系统会按配置频率生成图片。" />
      </q-tab-panel>

      <q-tab-panel name="recordings" class="detail-scroll-panel">
        <div class="detail-section-heading">
          <div><p>RECORDING FILES</p><h2>录像回放</h2></div>
          <q-badge color="primary" text-color="dark">{{ recordings.length }} 段</q-badge>
        </div>
        <q-banner v-if="isHEVC" dense rounded class="recording-codec-warning">当前录像为 H.265；若手机或浏览器无法解码，请使用 Windows adminService 回放。</q-banner>
        <section v-if="playingSegment" class="recording-stage">
          <video :key="playingSegment.id" controls autoplay playsinline :src="recordingURL(playingSegment.id)"></video>
          <div><strong>{{ localTime(playingSegment.started_at) }}</strong><q-btn flat dense icon="close" label="关闭播放" @click="playingSegment = undefined" /></div>
        </section>
        <q-list v-if="recordings.length" class="recording-list" separator>
          <q-item v-for="segment in recordings" :key="segment.id" clickable @click="playingSegment = segment">
            <q-item-section avatar><q-icon name="play_circle" color="primary" size="32px" /></q-item-section>
            <q-item-section><q-item-label>{{ localTime(segment.started_at) }}</q-item-label><q-item-label caption>{{ duration(segment.duration) }} · {{ fileSize(segment.file_size) }}</q-item-label></q-item-section>
            <q-item-section side><q-icon name="chevron_right" /></q-item-section>
          </q-item>
        </q-list>
        <EmptyState v-else icon="video_library" title="暂无录像" description="录像按10分钟分段，首段结束并完成索引后会显示。" />
      </q-tab-panel>

      <q-tab-panel name="services" class="detail-scroll-panel">
        <div class="detail-section-heading"><div><p>SERVICE STATUS</p><h2>当前服务配置</h2></div></div>
        <section class="detail-service-list">
          <article><q-icon name="fiber_smart_record" /><div><strong>录像存储</strong><span>{{ source.recording_enabled ? `已开启 · 保留${source.recording_retain_hours}小时` : '未开启' }}</span></div><q-badge :color="source.recording_enabled ? 'positive' : 'grey-7'">{{ source.recording_enabled ? '运行中' : '关闭' }}</q-badge></article>
          <article><q-icon name="photo_camera" /><div><strong>实时抽帧</strong><span>{{ source.sampling_enabled ? `已开启 · 每${source.sampling_interval_minutes}分钟${source.sampling_frame_count}帧` : '未开启' }}</span></div><q-badge :color="source.sampling_enabled ? 'positive' : 'grey-7'">{{ source.sampling_enabled ? '运行中' : '关闭' }}</q-badge></article>
        </section>
        <dl class="detail-device-meta">
          <div><dt>点位负责人</dt><dd>{{ source.operator_name || '未设置' }}</dd></div><div><dt>内网 IP</dt><dd>{{ source.local_ip || '-' }}</dd></div>
          <div><dt>主机名</dt><dd>{{ source.hostname || '-' }}</dd></div><div><dt>MAC</dt><dd>{{ source.mac || '-' }}</dd></div>
          <div><dt>编码</dt><dd>{{ source.codec || '-' }}</dd></div><div><dt>分辨率</dt><dd>{{ source.width ? `${source.width}×${source.height}` : '-' }}</dd></div>
        </dl>
      </q-tab-panel>
    </q-tab-panels>

    <q-dialog v-model="framePreviewOpen" maximized>
      <q-card dark class="frame-preview"><q-btn flat round icon="close" aria-label="关闭图片" @click="previewFrame = ''" /><img :src="previewFrame" alt="抽帧大图" /></q-card>
    </q-dialog>
  </q-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import EmptyState from './EmptyState.vue'
import LiveFeed from './LiveFeed.vue'
import { api, customerResourceURL, type FrameResult, type RecordingSegment, type VideoSource } from '../services/api'

const props = defineProps<{ source: VideoSource; server: string }>()
const emit = defineEmits<{ close: [] }>()
const $q = useQuasar()
const tab = ref('live')
const busy = ref(false)
const frames = ref<FrameResult[]>([])
const frameSources = ref<Record<number, string>>({})
const recordings = ref<RecordingSegment[]>([])
const playingSegment = ref<RecordingSegment>()
const previewFrame = ref('')
const framePreviewOpen = computed({ get: () => Boolean(previewFrame.value), set: (value) => { if (!value) previewFrame.value = '' } })
const isHEVC = computed(() => /hevc|h265/i.test(props.source.codec || ''))

function localTime(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN', { hour12: false }) }
function fileSize(value: number) { if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`; return `${(value / 1024 / 1024).toFixed(1)} MB` }
function duration(value: number) { const seconds = Math.max(0, Math.round(value)); return `${Math.floor(seconds / 60)}分${seconds % 60}秒` }
function frameURL(id: number) { return frameSources.value[id] || customerResourceURL(props.server, `/frames/${id}/image`) }
function recordingURL(id: number) { return customerResourceURL(props.server, `/segments/${id}/video`) }

async function loadResults() {
  busy.value = true
  const query = `?stream_name=${encodeURIComponent(props.source.stream_name)}`
  try {
    const [frameResult, segmentResult] = await Promise.all([
      api<FrameResult[]>(props.server, `/frames${query}`),
      api<{ segments: RecordingSegment[] }>(props.server, `/segments${query}`),
    ])
    frames.value = Array.isArray(frameResult) ? frameResult : []
    Object.values(frameSources.value).forEach((url) => URL.revokeObjectURL(url))
    frameSources.value = {}
    const token = localStorage.getItem('eyes_customer_session') || ''
    await Promise.all(frames.value.map(async (frame) => {
      try {
        const response = await fetch(customerResourceURL(props.server, `/frames/${frame.id}/image`), { headers: token ? { Authorization: `Bearer ${token}` } : {} })
        if (!response.ok) return
        frameSources.value[frame.id] = URL.createObjectURL(await response.blob())
      } catch { /* image remains unavailable */ }
    }))
    recordings.value = Array.isArray(segmentResult.segments) ? segmentResult.segments : []
  } catch (error) { $q.notify({ type: 'negative', message: (error as Error).message }) } finally { busy.value = false }
}

watch(() => props.source.video_source_id, () => { playingSegment.value = undefined; loadResults() })
onMounted(loadResults)
</script>
