const state = { rules: [], health: null, jobs: null, frames: [], streams: [] }
let livePlayer
let liveRetryTimer

const $ = (selector) => document.querySelector(selector)
const $$ = (selector) => [...document.querySelectorAll(selector)]

function escapeHTML(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;')
}

function localTime(value) {
    if (!value) return '-'
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN', { hour12: false })
}

function sourceType(value) {
    return { screen: '电脑桌面', usb_camera: 'USB摄像头', ip_camera: 'RTSP/NVR', direct_camera: '摄像头直推' }[value] || value || '-'
}

function codecName(value) {
    const normalized = String(value || '').trim().toUpperCase()
    if (normalized === 'HEVC' || normalized === 'H265' || normalized === 'H.265') return 'H.265'
    if (normalized === 'AVC' || normalized === 'H264' || normalized === 'H.264') return 'H.264'
    return normalized || '-'
}

function isHEVC(value) {
    return codecName(value) === 'H.265'
}

async function request(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.error || `请求失败 HTTP ${response.status}`)
    return body
}

function toast(message, error = false) {
    const node = $('#toast')
    node.textContent = message
    node.className = `toast show${error ? ' error' : ''}`
    clearTimeout(toast.timer)
    toast.timer = setTimeout(() => { node.className = 'toast' }, 3200)
}

function renderHealth() {
    const badge = $('#workerBadge')
    const health = state.health
    if (!health) {
        badge.className = 'worker-badge error'
        badge.lastElementChild.textContent = 'AI Worker连接失败'
        return
    }
    badge.className = `worker-badge ${health.ok ? 'online' : 'error'}`
    badge.lastElementChild.textContent = `${health.worker_id} · ${health.status === 'idle' ? '空闲' : health.status}`
}

function renderMetrics() {
    $('#enabledMetric').textContent = state.rules.filter((source) => source.enabled).length
    $('#frameMetric').textContent = state.rules.reduce((sum, source) => sum + Number(source.frame_count || 0), 0)
    const jobs = Array.isArray(state.jobs?.jobs) ? state.jobs.jobs : []
    const count = (status) => jobs.filter((item) => item.status === status).reduce((sum, item) => sum + Number(item.count || 0), 0)
    $('#runningMetric').textContent = count('running')
    $('#failedMetric').textContent = count('failed')
}

function renderSources() {
    const tbody = $('#sourceRows')
    if (!state.rules.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">尚无视频源。请先让客户端或摄像头推流。</td></tr>'
        return
    }
    tbody.innerHTML = state.rules.map((source) => `
        <tr>
            <td class="check-column"><input class="source-check" type="checkbox" data-id="${Number(source.video_source_id)}" ${source.enabled ? 'checked' : ''} aria-label="启用${escapeHTML(source.display_name)}抽帧" /></td>
            <td><span class="source-name">${escapeHTML(source.display_name || source.stream_name)}</span><span class="source-meta">${escapeHTML(source.stream_name)}</span></td>
            <td>${escapeHTML(sourceType(source.source_type))}</td>
            <td><span class="pill ${source.active ? 'online' : 'offline'}">${source.active ? '在线' : '离线'}</span></td>
            <td>${Number(source.frame_count || 0)} 张</td>
            <td>${escapeHTML(localTime(source.last_captured_at))}</td>
        </tr>`).join('')
    const firstEnabled = state.rules.find((source) => source.enabled)
    if (firstEnabled?.config?.frames_per_minute) $('#framesPerMinute').value = firstEnabled.config.frames_per_minute
    $('#selectAll').checked = state.rules.length > 0 && state.rules.every((source) => source.enabled)
    $$('.source-check').forEach((input) => input.addEventListener('change', syncSelectAll))
}

function renderSourceFilter() {
    const select = $('#resultSource')
    const current = select.value
    select.innerHTML = '<option value="">全部视频源</option>' + state.rules.map((source) =>
        `<option value="${escapeHTML(source.stream_name)}">${escapeHTML(source.display_name || source.stream_name)}</option>`
    ).join('')
    if ([...select.options].some((option) => option.value === current)) select.value = current
}

function selectedLiveStream() {
    const streamName = $('#liveSource').value
    return state.streams.find((stream) => stream.stream_name === streamName)
}

function updateLiveMetadata(stream) {
    $('#liveCodec').textContent = `编码 ${stream ? codecName(stream.codec) : '-'}`
    $('#liveResolution').textContent = stream?.width && stream?.height
        ? `分辨率 ${Number(stream.width)} × ${Number(stream.height)}`
        : '分辨率 -'
}

function setLiveStatus(message, error = false) {
    const status = $('#liveStatus')
    status.textContent = message
    status.className = `message${error ? ' error' : ''}`
}

function showLivePlaceholder(title, detail = '') {
    const placeholder = $('#livePlaceholder')
    placeholder.classList.remove('hidden')
    placeholder.firstElementChild.textContent = title
    placeholder.lastElementChild.textContent = detail
}

function hideLivePlaceholder() {
    $('#livePlaceholder').classList.add('hidden')
}

function destroyLivePlayer() {
    clearTimeout(liveRetryTimer)
    liveRetryTimer = undefined
    if (livePlayer) {
        try {
            livePlayer.pause()
            livePlayer.unload()
            livePlayer.detachMediaElement()
            livePlayer.destroy()
        } catch (error) {
            console.warn('[live] 销毁播放器失败', error)
        }
        livePlayer = undefined
    }
    const video = $('#liveVideo')
    video.onplaying = null
    video.onwaiting = null
    video.removeAttribute('src')
    video.load()
}

function renderLiveSources() {
    const select = $('#liveSource')
    const previous = select.value
    if (!state.streams.length) {
        select.innerHTML = '<option value="">当前没有在线流</option>'
        updateLiveMetadata(null)
        return
    }
    select.innerHTML = state.streams.map((stream) => {
        const name = stream.display_name || stream.stream_name
        const details = [codecName(stream.codec), stream.width && stream.height ? `${Number(stream.width)}×${Number(stream.height)}` : '']
            .filter(Boolean)
            .join(' · ')
        return `<option value="${escapeHTML(stream.stream_name)}">${escapeHTML(name)}（${escapeHTML(details)}）</option>`
    }).join('')
    if (state.streams.some((stream) => stream.stream_name === previous)) select.value = previous
    updateLiveMetadata(selectedLiveStream())
}

async function startLivePlayback() {
    const stream = selectedLiveStream()
    destroyLivePlayer()
    updateLiveMetadata(stream)
    if (!stream) {
        setLiveStatus('当前没有可播放的在线流')
        showLivePlaceholder('当前没有在线流', '请确认客户端或摄像头已经推流到SRS')
        return
    }
    if (!stream.playback_url) {
        setLiveStatus('SRS网页播放地址未配置', true)
        showLivePlaceholder('播放地址不可用', '请配置AI_SRS_PUBLIC_BASE')
        return
    }
    if (!window.mpegts) {
        setLiveStatus('mpegts.js加载失败', true)
        showLivePlaceholder('播放器加载失败', '请检查AIService播放器资源是否完整')
        return
    }

    const features = window.mpegts.getFeatureList()
    if (!features.mseLivePlayback) {
        setLiveStatus('当前Chrome不支持MSE实时流播放', true)
        showLivePlaceholder('浏览器不支持实时播放', '请升级Chrome并启用硬件加速')
        return
    }
    if (isHEVC(stream.codec) && !features.mseH265Playback) {
        setLiveStatus('当前电脑不支持H.265硬件解码', true)
        showLivePlaceholder('无法播放H.265', '请启用Chrome硬件加速、更新显卡驱动，或使用H.264子码流')
        return
    }

    const video = $('#liveVideo')
    showLivePlaceholder('正在连接实时视频…', `${codecName(stream.codec)} · HTTP-FLV`)
    setLiveStatus('正在连接SRS…')
    const player = window.mpegts.createPlayer(
        {
            type: 'flv',
            isLive: true,
            hasAudio: false,
            hasVideo: true,
            url: stream.playback_url,
        },
        {
            enableWorker: false,
            enableStashBuffer: false,
            stashInitialSize: 128,
            lazyLoad: false,
            autoCleanupSourceBuffer: true,
            autoCleanupMaxBackwardDuration: 10,
            autoCleanupMinBackwardDuration: 5,
            liveBufferLatencyChasing: true,
            liveBufferLatencyMaxLatency: 1.5,
            liveBufferLatencyMinRemain: 0.3,
        },
    )
    livePlayer = player
    video.onplaying = () => {
        if (livePlayer !== player) return
        hideLivePlaceholder()
        setLiveStatus(`正在播放 ${stream.display_name || stream.stream_name}`)
    }
    video.onwaiting = () => {
        if (livePlayer === player) setLiveStatus('网络缓冲中…')
    }
    player.on(window.mpegts.Events.ERROR, (type, detail, info) => {
        if (livePlayer !== player) return
        const errorDetail = info?.msg || detail || type || '未知错误'
        setLiveStatus(`播放中断：${String(errorDetail)}`, true)
        showLivePlaceholder('视频连接中断', '3秒后自动重连')
        clearTimeout(liveRetryTimer)
        liveRetryTimer = setTimeout(() => {
            if ($('#livePage').classList.contains('active') && $('#liveSource').value === stream.stream_name) {
                startLivePlayback().catch((error) => setLiveStatus(error.message, true))
            }
        }, 3000)
    })
    player.attachMediaElement(video)
    player.load()
    await player.play().catch(() => {})
}

async function loadStreams(autoplay = false) {
    const payload = await request('/api/dashboard/streams')
    state.streams = Array.isArray(payload.streams)
        ? payload.streams.filter((stream) => stream?.active !== false && stream?.stream_name)
        : []
    renderLiveSources()
    if (autoplay) await startLivePlayback()
}

function renderFrames() {
    const grid = $('#resultGrid')
    if (!state.frames.length) {
        grid.innerHTML = '<p class="empty">当前筛选条件下还没有抽帧结果。</p>'
        return
    }
    grid.innerHTML = state.frames.map((frame) => `
        <article class="result-card">
            <a href="/api/dashboard/frames/${Number(frame.id)}/image" target="_blank" rel="noopener">
                <img src="/api/dashboard/frames/${Number(frame.id)}/image" loading="lazy" alt="${escapeHTML(frame.display_name)}抽帧结果" />
            </a>
            <div class="result-info">
                <strong>${escapeHTML(frame.display_name || frame.stream_name)}</strong>
                <span>${escapeHTML(localTime(frame.captured_at))}</span>
                <span>实时截图 · ${(Number(frame.file_size || 0) / 1024).toFixed(1)} KB</span>
            </div>
        </article>`).join('')
}

function syncSelectAll() {
    const boxes = $$('.source-check')
    $('#selectAll').checked = boxes.length > 0 && boxes.every((box) => box.checked)
}

async function loadRules() {
    const payload = await request('/api/dashboard/rules')
    state.rules = Array.isArray(payload.sources) ? payload.sources : []
    renderSources()
    renderSourceFilter()
    renderMetrics()
}

async function loadRuntime() {
    const [health, jobs] = await Promise.all([
        request('/health').catch(() => null),
        request('/api/dashboard/jobs').catch(() => null),
    ])
    state.health = health
    state.jobs = jobs
    renderHealth()
    renderMetrics()
}

async function loadFrames() {
    const params = new URLSearchParams()
    if ($('#resultSource').value) params.set('stream_name', $('#resultSource').value)
    if ($('#resultDate').value) params.set('date', $('#resultDate').value)
    $('#resultGrid').innerHTML = '<p class="empty">正在读取抽帧结果…</p>'
    state.frames = await request(`/api/dashboard/frames?${params}`)
    renderFrames()
}

async function saveRules() {
    const rate = Number.parseInt($('#framesPerMinute').value, 10)
    if (!Number.isInteger(rate) || rate < 1 || rate > 60) {
        toast('每分钟抽帧数必须在1到60之间', true)
        return
    }
    const enabledIds = $$('.source-check:checked').map((input) => Number(input.dataset.id))
    const button = $('#saveRulesButton')
    const message = $('#saveMessage')
    button.disabled = true
    message.className = 'message'
    message.textContent = '正在保存规则并创建抽帧任务…'
    try {
        await request('/api/dashboard/rules', {
            method: 'PUT',
            body: JSON.stringify({ algorithm_code: 'frame_sampler', enabled_source_ids: enabledIds, config: { frames_per_minute: rate } }),
        })
        message.className = 'message success'
        message.textContent = `已启用 ${enabledIds.length} 个实时视频源，每分钟抽取 ${rate} 帧；不受录像开关影响。`
        toast('抽帧规则已保存并立即生效')
        await loadRules()
    } catch (error) {
        message.className = 'message error'
        message.textContent = error.message
        toast(error.message, true)
    } finally {
        button.disabled = false
    }
}

function bindEvents() {
    $$('.tab').forEach((button) => button.addEventListener('click', () => {
        $$('.tab').forEach((tab) => tab.classList.toggle('active', tab === button))
        $$('.page').forEach((page) => page.classList.remove('active'))
        $(`#${button.dataset.page}Page`).classList.add('active')
        if (button.dataset.page === 'results') loadFrames().catch((error) => toast(error.message, true))
        if (button.dataset.page === 'live') {
            loadStreams(true).catch((error) => {
                setLiveStatus(error.message, true)
                showLivePlaceholder('读取在线流失败', error.message)
                toast(error.message, true)
            })
        } else {
            destroyLivePlayer()
        }
    }))
    $('#selectAll').addEventListener('change', (event) => {
        $$('.source-check').forEach((input) => { input.checked = event.target.checked })
    })
    $('#selectOnlineButton').addEventListener('click', () => {
        const online = new Set(state.rules.filter((source) => source.active).map((source) => String(source.video_source_id)))
        $$('.source-check').forEach((input) => { input.checked = online.has(input.dataset.id) })
        syncSelectAll()
    })
    $('#saveRulesButton').addEventListener('click', saveRules)
    $('#refreshResultsButton').addEventListener('click', () => loadFrames().catch((error) => toast(error.message, true)))
    $('#refreshStreamsButton').addEventListener('click', () => {
        loadStreams(true).catch((error) => toast(error.message, true))
    })
    $('#liveSource').addEventListener('change', () => {
        startLivePlayback().catch((error) => setLiveStatus(error.message, true))
    })
    window.addEventListener('beforeunload', destroyLivePlayer)
}

async function initialize() {
    bindEvents()
    try {
        await Promise.all([loadRules(), loadRuntime()])
        await loadStreams()
        await loadFrames()
    } catch (error) {
        toast(error.message, true)
        $('#sourceRows').innerHTML = `<tr><td colspan="6" class="empty">${escapeHTML(error.message)}</td></tr>`
    }
    setInterval(loadRuntime, 10000)
}

initialize()
