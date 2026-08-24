<template>
  <div v-if="!authenticated" class="login-screen">
    <div class="brand-mark"><span></span><span></span><span></span></div>
    <p class="overline">ALL-SEEING EYES</p>
    <h1>千里眼</h1>
    <p class="login-subtitle">客户视频与智能服务平台</p>
    <q-form class="login-form" @submit="login">
      <q-input v-model="server" dark outlined label="服务器地址" autocomplete="url" />
      <q-input v-model="username" dark outlined label="账号" autocomplete="username" />
      <q-input v-model="password" dark outlined label="密码" type="password" autocomplete="current-password" />
      <q-btn unelevated color="primary" text-color="dark" class="full-width login-button" label="登录" type="submit" :loading="busy" />
      <p v-if="errorMessage" class="form-error">{{ errorMessage }}</p>
    </q-form>
    <small class="login-foot">安全连接 · 多设备统一管理</small>
  </div>

  <q-layout v-else view="lHh Lpr lFf" class="customer-layout">
    <q-header class="app-header">
      <q-toolbar>
        <div>
          <p class="header-kicker">{{ customerName }}</p>
          <q-toolbar-title>{{ pageTitle }}</q-toolbar-title>
        </div>
        <q-space />
        <q-btn flat round icon="refresh" aria-label="刷新" :loading="busy" @click="refreshCurrent" />
      </q-toolbar>
    </q-header>

    <q-page-container>
      <q-page v-show="tab === 'live'" class="live-page">
        <div v-if="onlineSources.length" ref="feedScroller" class="feed-scroller">
          <LiveFeed v-for="(source, index) in onlineSources" :key="source.video_source_id" :source="source" :active="index === activeFeed" />
        </div>
        <EmptyState v-else icon="videocam_off" title="暂无在线视频" description="设备开始推流后，画面会显示在这里。" />
      </q-page>

      <q-page v-show="tab === 'devices'" padding class="content-page">
        <section class="summary-card">
          <div><span>全部点位</span><strong>{{ sources.length }}</strong></div>
          <div><span>在线</span><strong class="positive-text">{{ onlineCount }}</strong></div>
          <div><span>离线</span><strong>{{ sources.length - onlineCount }}</strong></div>
        </section>
        <div class="section-title"><div><p>DEVICE POINTS</p><h2>设备点位</h2></div></div>
        <div class="device-list">
          <article v-for="source in sources" :key="source.video_source_id" class="device-card">
            <div class="device-icon"><q-icon :name="source.source_type === 'screen' ? 'desktop_windows' : 'videocam'" /></div>
            <div class="device-main"><strong>{{ source.display_name || source.stream_name }}</strong><span>{{ sourceType(source.source_type) }} · {{ source.brand || '通用设备' }}</span><small>{{ source.stream_name }}</small></div>
            <q-badge :color="source.active ? 'positive' : 'grey-7'" rounded>{{ source.active ? '在线' : '离线' }}</q-badge>
            <dl><div><dt>编码</dt><dd>{{ codec(source.codec) }}</dd></div><div><dt>分辨率</dt><dd>{{ source.width ? `${source.width}×${source.height}` : '-' }}</dd></div><div><dt>MAC</dt><dd>{{ source.mac || '-' }}</dd></div><div><dt>设备编号</dt><dd>{{ source.source_id || '-' }}</dd></div></dl>
          </article>
        </div>
      </q-page>

      <q-page v-show="tab === 'services'" padding class="content-page">
        <div class="service-hero"><p>AI & STORAGE</p><h2>点位服务配置</h2><span>录像与实时抽帧彼此独立，可按点位分别开启。</span></div>
        <div class="service-list">
          <article v-for="source in sources" :key="source.video_source_id" class="service-card">
            <header><div><strong>{{ source.display_name || source.stream_name }}</strong><span>{{ source.active ? '当前在线' : '当前离线' }}</span></div><q-icon :name="source.source_type === 'screen' ? 'desktop_windows' : 'videocam'" /></header>
            <div class="service-row"><div><q-icon name="fiber_smart_record" /><span><strong>录像存储</strong><small>分段保存完整视频</small></span></div><q-toggle v-model="source.recording_enabled" color="primary" /></div>
            <q-slide-transition><div v-show="source.recording_enabled" class="option-row"><span>录像保留天数</span><q-input v-model.number="source.recording_retain_days" dense outlined dark type="number" min="1" max="3650" suffix="天" /></div></q-slide-transition>
            <div class="service-row"><div><q-icon name="photo_camera" /><span><strong>实时抽帧</strong><small>从在线流定时保存图片</small></span></div><q-toggle v-model="source.sampling_enabled" color="primary" /></div>
            <q-slide-transition><div v-show="source.sampling_enabled" class="option-row"><span>每分钟抽帧</span><q-input v-model.number="source.frames_per_minute" dense outlined dark type="number" min="1" max="60" suffix="帧" /></div></q-slide-transition>
          </article>
        </div>
        <q-btn unelevated color="primary" text-color="dark" class="save-services" icon="check" label="保存全部配置" :loading="busy" @click="saveServices" />
      </q-page>

      <q-page v-show="tab === 'profile'" padding class="content-page profile-page">
        <section class="profile-card">
          <div class="avatar">{{ accountInitial }}</div><div><p>{{ customerName }}</p><h2>{{ me?.username }}</h2><span>客户管理员</span></div>
        </section>
        <section class="profile-stats"><div><strong>{{ sources.length }}</strong><span>管理点位</span></div><div><strong>{{ onlineCount }}</strong><span>在线点位</span></div><div><strong>{{ serviceCount }}</strong><span>已开服务</span></div></section>
        <q-list class="profile-list" separator>
          <q-item><q-item-section avatar><q-icon name="badge" /></q-item-section><q-item-section><q-item-label>登录账号</q-item-label><q-item-label caption>{{ me?.username }}</q-item-label></q-item-section></q-item>
          <q-item clickable @click="passwordDialog = true"><q-item-section avatar><q-icon name="lock_reset" /></q-item-section><q-item-section>修改密码</q-item-section><q-item-section side><q-icon name="chevron_right" /></q-item-section></q-item>
          <q-item><q-item-section avatar><q-icon name="dns" /></q-item-section><q-item-section><q-item-label>当前服务器</q-item-label><q-item-label caption>{{ server }}</q-item-label></q-item-section></q-item>
        </q-list>
        <q-btn outline color="negative" class="full-width logout-button" icon="logout" label="退出登录" @click="logout" />
      </q-page>
    </q-page-container>

    <q-footer class="bottom-nav">
      <q-tabs v-model="tab" dense no-caps indicator-color="transparent" active-color="primary">
        <q-tab name="live" icon="smart_display" label="实时" />
        <q-tab name="devices" icon="devices_other" label="设备" />
        <q-tab name="services" icon="auto_awesome" label="AI服务" />
        <q-tab name="profile" icon="person" label="我的" />
      </q-tabs>
    </q-footer>
  </q-layout>

  <q-dialog v-model="passwordDialog">
    <q-card dark class="password-card"><q-card-section><div class="text-h6">修改登录密码</div><div class="text-caption text-grey-5">修改后需要重新登录所有设备。</div></q-card-section><q-card-section class="q-gutter-md"><q-input v-model="currentPassword" dark outlined type="password" label="当前密码" /><q-input v-model="newPassword" dark outlined type="password" label="新密码（8～72位）" /></q-card-section><q-card-actions align="right"><q-btn flat label="取消" v-close-popup /><q-btn color="primary" text-color="dark" label="确认修改" :loading="busy" @click="changePassword" /></q-card-actions></q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import LiveFeed from '../components/LiveFeed.vue'
import EmptyState from '../components/EmptyState.vue'
import { api, clearSession, defaultServer, saveServer, type AccountUser, type VideoSource } from '../services/api'

const $q = useQuasar()
const server = ref(defaultServer())
const username = ref('')
const password = ref('')
const me = ref<AccountUser>()
const sources = ref<VideoSource[]>([])
const tab = ref('live')
const busy = ref(false)
const errorMessage = ref('')
const feedScroller = ref<HTMLElement>()
const activeFeed = ref(0)
const passwordDialog = ref(false)
const currentPassword = ref('')
const newPassword = ref('')
let observer: IntersectionObserver | undefined

const authenticated = computed(() => Boolean(me.value))
const customerName = computed(() => me.value?.customer_name || '客户平台')
const accountInitial = computed(() => customerName.value.slice(0, 1))
const onlineSources = computed(() => sources.value.filter((source) => source.active && source.playback_url))
const onlineCount = computed(() => sources.value.filter((source) => source.active).length)
const serviceCount = computed(() => sources.value.reduce((sum, source) => sum + Number(source.recording_enabled) + Number(source.sampling_enabled), 0))
const pageTitle = computed(() => ({ live: '实时视频', devices: '设备管理', services: 'AI服务', profile: '个人中心' }[tab.value] || '客户平台'))

function sourceType(value: string) { return ({ screen: '电脑桌面', usb_camera: 'USB摄像头', ip_camera: 'RTSP/NVR', direct_camera: '网络摄像头' } as Record<string, string>)[value] || '视频点位' }
function codec(value: string) { return /hevc|h265/i.test(value) ? 'H.265' : /avc|h264/i.test(value) ? 'H.264' : value || '-' }

async function login() {
  busy.value = true; errorMessage.value = ''
  try {
    saveServer(server.value)
    const result = await api<{ user: AccountUser }>(server.value, '/auth/login', { method: 'POST', body: JSON.stringify({ username: username.value.trim(), password: password.value }) })
    me.value = result.user
    password.value = ''
    await loadSources()
  } catch (error) { errorMessage.value = (error as Error).message } finally { busy.value = false }
}

async function restoreSession() {
  try { const result = await api<{ user: AccountUser }>(server.value, '/auth/me'); me.value = result.user; await loadSources() } catch { clearSession(); me.value = undefined }
}

async function loadSources() {
  const [sourceResult, streamResult] = await Promise.all([
    api<{ sources: VideoSource[] }>(server.value, '/sources'),
    api<{ streams: VideoSource[] }>(server.value, '/streams'),
  ])
  const playback = new Map(streamResult.streams.map((stream) => [stream.stream_name, stream.playback_url]))
  sources.value = sourceResult.sources.map((source) => ({ ...source, playback_url: playback.get(source.stream_name) || '' }))
  await setupFeedObserver()
}

async function refreshCurrent() {
  busy.value = true
  try { await loadSources(); $q.notify({ type: 'positive', message: '数据已刷新' }) } catch (error) { $q.notify({ type: 'negative', message: (error as Error).message }) } finally { busy.value = false }
}

async function saveServices() {
  const invalid = sources.value.some((source) => source.recording_retain_days < 1 || source.recording_retain_days > 3650 || source.frames_per_minute < 1 || source.frames_per_minute > 60)
  if (invalid) return $q.notify({ type: 'negative', message: '保留天数为1～3650，抽帧频率为1～60' })
  busy.value = true
  try {
    await api(server.value, '/sources', { method: 'PUT', body: JSON.stringify({ sources: sources.value.map((source) => ({ video_source_id: source.video_source_id, recording_enabled: source.recording_enabled, recording_retain_days: source.recording_retain_days, sampling_enabled: source.sampling_enabled, frames_per_minute: source.frames_per_minute })) }) })
    $q.notify({ type: 'positive', message: '所有点位服务配置已生效' })
    await loadSources()
  } catch (error) { $q.notify({ type: 'negative', message: (error as Error).message }) } finally { busy.value = false }
}

async function changePassword() {
  if (newPassword.value.length < 8 || newPassword.value.length > 72) return $q.notify({ type: 'negative', message: '新密码长度必须为8～72位' })
  busy.value = true
  try {
    await api(server.value, '/auth/password', { method: 'PUT', body: JSON.stringify({ current_password: currentPassword.value, new_password: newPassword.value }) })
    passwordDialog.value = false
    $q.notify({ type: 'positive', message: '密码已修改，请重新登录' })
    await logout(false)
  } catch (error) { $q.notify({ type: 'negative', message: (error as Error).message }) } finally { busy.value = false }
}

async function logout(callServer = true) {
  if (callServer) { try { await api(server.value, '/auth/logout', { method: 'POST', body: '{}' }) } catch { /* local logout */ } }
  clearSession(); me.value = undefined; sources.value = []; currentPassword.value = ''; newPassword.value = ''
}

async function setupFeedObserver() {
  observer?.disconnect()
  await nextTick()
  if (!feedScroller.value) return
  const cards = [...feedScroller.value.querySelectorAll('.live-feed')]
  observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0]
    if (visible) activeFeed.value = cards.indexOf(visible.target as HTMLElement)
  }, { root: feedScroller.value, threshold: [0.55, 0.8] })
  cards.forEach((card) => observer?.observe(card))
}

watch(tab, (value) => { if (value === 'live') setupFeedObserver() })
onMounted(restoreSession)
onBeforeUnmount(() => observer?.disconnect())
</script>
