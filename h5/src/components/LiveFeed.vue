<template>
  <article class="live-feed">
    <video ref="video" muted playsinline controls></video>
    <div v-if="status !== 'playing'" class="video-overlay">
      <q-spinner-puff v-if="status === 'loading'" color="primary" size="48px" />
      <q-icon v-else :name="status === 'offline' ? 'videocam_off' : 'error_outline'" size="42px" />
      <strong>{{ statusText }}</strong>
      <span>{{ detailText }}</span>
    </div>
    <div class="feed-gradient"></div>
    <div class="feed-copy">
      <div><q-badge :color="source.active ? 'positive' : 'grey-7'" rounded>{{ source.active ? '在线' : '离线' }}</q-badge></div>
      <h2>{{ source.display_name || source.stream_name }}</h2>
      <p>{{ sourceTypeLabel }} · {{ codecLabel }}<span v-if="source.width"> · {{ source.width }}×{{ source.height }}</span></p>
      <small>上滑查看下一点位</small>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { VideoSource } from '../services/api'

type MpegtsModule = typeof import('mpegts.js')['default']

const props = defineProps<{ source: VideoSource; active: boolean }>()
const video = ref<HTMLVideoElement>()
const status = ref<'idle' | 'loading' | 'playing' | 'offline' | 'error'>('idle')
const errorText = ref('')
let player: ReturnType<MpegtsModule['createPlayer']> | undefined
let lifecycle = 0

const sourceTypeLabel = computed(() => ({ screen: '电脑桌面', usb_camera: 'USB摄像头', ip_camera: 'RTSP/NVR', direct_camera: '网络摄像头' }[props.source.source_type] || '视频点位'))
const codecLabel = computed(() => /hevc|h265/i.test(props.source.codec) ? 'H.265' : /avc|h264/i.test(props.source.codec) ? 'H.264' : (props.source.codec || '编码未知'))
const statusText = computed(() => status.value === 'offline' ? '点位当前离线' : status.value === 'error' ? '暂时无法播放' : status.value === 'loading' ? '正在连接视频…' : '等待播放')
const detailText = computed(() => status.value === 'error' ? errorText.value : status.value === 'offline' ? '设备恢复推流后可继续查看' : codecLabel.value)

function stop() {
  lifecycle += 1
  if (player) {
    try { player.pause(); player.unload(); player.detachMediaElement(); player.destroy() } catch { /* disposed */ }
    player = undefined
  }
  if (video.value) { video.value.removeAttribute('src'); video.value.load() }
  status.value = 'idle'
}

async function start() {
  stop()
  const currentLifecycle = lifecycle
  if (!props.active) return
  if (!props.source.active || !props.source.playback_url) { status.value = 'offline'; return }
  const mpegts = (await import('mpegts.js')).default
  if (currentLifecycle !== lifecycle || !props.active) return
  if (!mpegts.isSupported() || !video.value) { status.value = 'error'; errorText.value = '当前设备不支持HTTP-FLV播放'; return }
  const features = mpegts.getFeatureList()
  if (/hevc|h265/i.test(props.source.codec) && !features.mseH265Playback) {
    status.value = 'error'; errorText.value = '当前设备不支持H.265硬件解码'; return
  }
  status.value = 'loading'
  player = mpegts.createPlayer({ type: 'flv', isLive: true, hasAudio: false, url: props.source.playback_url }, {
    enableStashBuffer: false, stashInitialSize: 128, lazyLoad: false,
    autoCleanupSourceBuffer: true, liveBufferLatencyChasing: true,
  })
  player.on(mpegts.Events.ERROR, (_type, detail) => { status.value = 'error'; errorText.value = String(detail || '视频连接中断') })
  player.attachMediaElement(video.value)
  player.load()
  video.value.onplaying = () => { status.value = 'playing' }
  await player.play().catch(() => {})
}

onMounted(start)
watch(() => [props.active, props.source.playback_url, props.source.active], start, { flush: 'post' })
onBeforeUnmount(stop)
</script>
